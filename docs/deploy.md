# 升级与部署

本文写给要换服务运行的代码或配置的人：改了开关要重新注册、升级到新版本、出事回滚，以及自己维护固定 release 时部署脚本一步步做了什么。服务由哪些进程组成、数据放在哪里、日常的启停见[运维指南](operations.md)；开关和环境变量见[配置](configuration.md)。

命令都在服务正在运行的代码目录（`./install` 的检出，或 `releases/` 下的 release 目录）里执行，并假设已按[运维 · 找到数据根和 APP_ID](operations.md#找到数据根和-app_id)设好 `COVEY_DATA_HOME`、`APP_ID`、`STATE_DIR` 和 `SERVICE_ROOT`。

| 要做的事 | 先做 | 再看 |
| --- | --- | --- |
| 改开关 | | `covey_service.py set`，见[配置 · 改开关三步](configuration.md#改开关三步) |
| 改 Codex 参数 | [载入服务清单的环境](#载入服务清单的环境) | [改设置后重新注册](#改设置后重新注册) |
| 升级到新版本 | [载入服务清单的环境](#载入服务清单的环境) | [升级与回滚](#升级与回滚) |
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
export COVEY_DIGEST_TIMES=09:30,21:30
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

## 升级与回滚

升级的做法只有一种：用 `scripts/deploy_bridge.sh` 把要部署的提交导出成 `$COVEY_DATA_HOME/releases/<id>/` 下的固定 release，再让服务从它运行。不要在服务运行的检出里 `git pull` 了事：`./install` 装的服务直接从检出运行，拉代码之后新开的任务进程立刻用新代码，Bridge 却还是旧的，直到下次重启。固定 release 不会被之后的 `git pull` 改动，旧 release 留着就能回滚。

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
- 预检里最常见的是 `env_missing`、`env_extra`、`env_value_diff`：终端和清单的变量不一致，按上面重新载入。要改开关的，先按[改设置后重新注册](#改设置后重新注册)改完，再升级。
- 部署后它自己核对新 Bridge 起来、事件就绪、`covey_doctor --runtime-only` 通过、原来在跑的任务都被接管；`post.app_config` 报飞书应用还缺什么（不算失败，补法见[飞书应用配置](feishu-app.md)）。
- 已知限制：脚本开头会 `unset HTTPS_PROXY`，所以清单里有大写 `HTTPS_PROXY` 的服务预检总报 `env_missing`。现在的 `./install` 把代理存成小写的 `https_proxy`；更早装的服务（在 `./install` 里输入过代理地址，或终端里设的是大写）清单里还是大写。先在载入清单的终端里 `export https_proxy="$HTTPS_PROXY"; unset HTTPS_PROXY`，按[改设置后重新注册](#改设置后重新注册)注册一次，代理就改存成小写的 `https_proxy`，之后照常部署。

**回滚**：换回上一对提交，加 `--allow-rollback`（不加时预检拦下比现网旧的提交）：

```bash
ls "$COVEY_DATA_HOME/releases/"          # 上一个 release；带扩展的看它的 RELEASE_MANIFEST 里各检出的提交
scripts/deploy_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" --run-dir "$RUN" \
  --commit <上一个核心提交> [--ext-repo /absolute/<扩展检出> --ext-commit <上一个扩展提交>] --allow-rollback
```

release 目录已存在时不重新打包，只重新注册，所以回滚很快。旧 release 要留到用它的任务进程都结束（见下面第 5 步）。开了多设备时回滚的注意事项见[运维 · 多设备：设备上的任务没停掉](operations.md#多设备设备上的任务没停掉)第 5 条。

## 部署新版本

从固定的 release 目录运行，更新代码时不会改变正在运行的服务和任务进程用的文件。

`scripts/deploy_bridge.sh --workdir <目录> --profile <profile> [-- <bridge 参数>]` 按下面第 1–4 步做完并打印一行 JSON，检查不过就停；`--check-only` 只做部署前的检查，参数见脚本开头。其中权限变量（`pre.permission_env`）查目标服务的清单、当前终端和服务根下其他运行中的服务的清单，和部署后 `covey_doctor --runtime-only` 的口径一致（停用的服务只告警）。部署后的 `covey_doctor --runtime-only` 不是 0 时每 10 秒再问一次，最多 90 秒（刚启动时事件连接还在建立，会短暂报 degraded），`post.doctor_tries` 是问了几次。它还在结果里报 `post.app_config`（飞书应用缺不缺东西），不算失败。

### 一次只有一个部署，不往回部署

几个人或几个 Agent 都可能部署同一个服务，同时跑会互相覆盖，后跑完的生效，较旧的提交还可能盖掉较新的现网。所以：

- **排队**：真部署在部署前检查之前拿服务根下的 `deploy.lock`（`flock`，进程退出就释放，崩溃也一样；NFS 上同样互斥），一直持到结束；锁文件里写着谁拿着（`pid`、`host`、`commit`、`release_id`、`started`、`run_dir`、`task`）。别的部署正拿着时立刻退出，退出码 3，结果里的 `deploy_running` 是那位占用者，什么都不碰。等它部署完再跑一次就是排队。`--check-only` 不拿锁、不受它拦，只在 `pre.deploy_running` 里报有没有部署在跑（没有是 `null`）。
- **不回退**：现网服务清单里的入口在 `$COVEY_DATA_HOME/releases/<id>/` 下时，现网的提交（release 里的 `RELEASE_MANIFEST`，没有就按 id）必须是 `--commit` 的祖先；扩展个数和现网一样时，每个 `--ext-commit` 也按顺序这样比。不是就拦下（`pre.rollback` 写明哪个提交没包含现网的哪个，或检出里没有现网的提交、要先 fetch），`--check-only` 也报。要部署的提交是从旧 `main` 拿的，先同步最新 `main`；真要回滚时加 `--allow-rollback`。现网不是从 `releases/` 下运行的（如 `./install` 直接跑检出）不比，`pre.live_release` 是 `null`。

### 核心和扩展分开检出

核心在 `covey/` 检出，扩展在各自的库里（扩展库根下每个含 manifest 文件的目录是一个扩展包）。加 `--ext-repo <扩展检出>` 和 `--ext-commit <提交>`（默认 `HEAD`），都可重复，按先后配对（有几个扩展库就加几对）。`--repo`/`--commit` 仍是核心。release 的 `extensions/enabled` 列每个扩展检出根目录 `enabled` 文件里的包（没有这个文件就列它所有的包）；检出里没列的包随 release 带着但默认不启用，用 `covey_plugins.py enable` 打开（见[运维 · 扩展的装和卸](operations.md#扩展的装和卸)）。

```bash
scripts/deploy_bridge.sh --workdir <目录> --profile <profile> --repo /absolute/covey --ext-repo /absolute/covey-internal --check-only
```

- 打包由 `scripts/assemble_release.py` 做：核心 `git archive` 到 release，每个扩展包 `git archive` 到 `release/extensions/<包名>/`，去掉包里的 `standalone/`，包的 `bin/*.py` 入口（如扩展自己的运行时脚本）放进 `release/bin/` 盖掉核心里的转发脚本，写 `extensions/enabled`（按 `--ext-repo` 的顺序列出所有包名）和 `RELEASE_MANIFEST`（各检出的完整提交号），再重写 `AGENTS.md` 的模块表。形状和单仓库的 release 相同，服务清单、`runs/<任务键>/` 协议、回滚都不变。拼装按提交取，不看检出的工作区。
- release id 是 `<核心前 8 位>-<扩展前 8 位>[-<第二个扩展前 8 位>…]`，release 目录、`bridge started … release=<id>` 的匹配都用它；结果 JSON 的 `commit` 仍是核心完整提交号，另有 `ext_commits` 和 `release_id`。不给 `--ext-repo` 时 release 目录名是核心提交号。回滚只回一半：核心提交不变、`--ext-commit` 设成旧值，得到一个新 id 的 release。
- 额外的预检（`--check-only` 同样做）：扩展检出是 git 仓库、提交存在（否则退出码 2）；扩展库的 `core.lock` 写的核心提交和 `--commit` 不一致时拦下（`pre.core_lock_mismatch`；试新核心时加 `--allow-core-mismatch` 放行）；服务清单和当前终端里不能有 `COVEY_EXTENSION_PATH`、`COVEY_EXTENSIONS`（`pre.extension_env`；它们会盖过 release 里的 `extensions/`，unset 后重新注册）；启用的扩展自己的检查要过（`pre.extension_checks`，说明在 `pre.extension_notes`）：脚本把要部署的 release 拼到运行目录的 `tmp/`（release 已存在时直接用它），在里面跑 `bin/covey_plugins.py deploy-check --harness-config <配置>`，跑完删掉，所以扩展看到的是这次要部署的代码和服务实际用的配置（例如隔离解释器的源码是不是 release 固定的版本）；确认无碍时加 `--allow-extension-checks` 放行。回滚到还没有 `deploy-check` 的旧核心时不检查，`pre.extension_notes` 里说明。

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
- 部署前在跑的每个任务都有一行 `adopted task runner job=#N pid=…`，任务卡的执行过程里多一条“机器人 HH:MM 重启过，任务未中断。”。
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

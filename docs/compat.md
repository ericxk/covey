# 改名与兼容

本文写给从老部署升级的人和写外部扩展的人：产品改名 Covey 后哪些旧名字还能用、新旧版本混跑时互相认得什么、老的安装形态怎么处理。新装的部署用不到这一篇。已经下线的功能和不再读取的变量见[已下线的功能与旧配置](legacy.md)。

## 改名 Covey 后的兼容

产品和包改名为 Covey，所有现有部署不改配置照常升级；下面这些旧名都还能用，保留至少一个版本。

| 旧 | 新 | 兼容做法 |
| --- | --- | --- |
| Python 包 `harness_bridge` | `covey` | 仓库根的 `harness_bridge.py`（单文件，不是包，`covey/__init__.py` 仍为空）装一个导入别名：`harness_bridge.X` 就是 `covey.X` 同一个模块对象，首次导入在 stderr 记一行弃用。给外部扩展用 |
| `LARK_BRIDGE_<X>` | `COVEY_<X>` | 两个名字都读，见下面[环境变量的旧名](#环境变量的旧名) |
| `bin/lark_harness_bridge.py`、`lark_task_runner.py`、`bridge_service.py`、`bridge_service_install.py`、`bridge_doctor.py`、`bridge_memguard.py`、`bridge_pairing.py`、`bridge_main_mcp.py`、`covey_main_mcp.py`（原来的入口，也成了转发壳）、`bridge_app_setup.py`、`lark_bridge_paths.py`、`lark_bridge_maintenance.py`、`lark_bridge_workspaces.py`、`lark_app_console.py`、`lark_feishu_web.py`、`lark_install_browser.py`、`lark_workspace_reader.py`（扩展的入口见扩展 README，同样留了旧名转发壳） | `bin/covey_bridge.py`、`covey_runner.py`、`covey_service.py`、`covey_service_install.py`、`covey_doctor.py`、`covey_memguard.py`、`covey_pairing.py`、`covey_butler_mcp.py`、`covey_app_setup.py`、`covey_paths.py`、`covey_maintenance.py`、`covey_workspaces.py`、`covey_app_console.py`、`covey_feishu_web.py`、`covey_install_browser.py`、`covey_workspace_reader.py`（`covey_install.py` 本来就是新名） | 旧名留作两行转发壳（`runpy.run_path` 新名），服务清单、开机钩子、crontab、systemd 单元、各部署的脚本里写死的旧路径继续有效；新注册（bootstrap、installer、`bridge_service_install` 写的单元和 crontab 行）用新名 |
| `lark-bridge-manager.service` | `covey-manager.service` | 新装用新名；已装旧单元的机器继续用旧单元名（`unit_name()` 看 `~/.config/systemd/user/` 里有没有旧文件），不停不换；单元里的 `ExecStart` 下次装时改成新入口 |
| 抽象套接字 `\0lark-bridge-…`、`~/.config/lark-bridge-service/`、`~/.local/libexec/lark-bridge/memguard.py`、crontab 标记 | 不变 | 新旧 Manager、看门狗要互相认得，数据和注册位置都不改名 |
| `scripts/setup_harness_bridge.sh`、`scripts/bootstrap_harness_bridge.sh` | 不变 | `./install` 和 `deploy_bridge.sh` 都调 bootstrap；旧安装脚本 setup 仍可用，不再推荐 |

数据目录（`COVEY_DATA_HOME`/`LARK_BRIDGE_DATA_HOME` 指的目录、`harness-<APP_ID>` 状态目录、`releases/`、`bridge-services/`、`runs/<任务键>/` 里的文件名）一个都没改；状态目录里的 `main-thread.json`、`main-thread/` 也沿用旧名。`deploy_bridge.sh` 比的是清单里的命令参数，不比入口脚本的路径，所以现网清单写着旧入口时不会因为命令变了而拦。

## 环境变量的旧名

设置改名为 `COVEY_<X>` 后，旧名 `LARK_BRIDGE_<X>` 和管家改名前的 `MAIN_*` 写法照常生效（读取顺序见[配置 · 环境变量与选项一览](configuration.md#环境变量与选项一览)）。为了升级和回滚都不用改配置：

- **服务清单**：`runtime_environment()` 仍按旧名存（`COVEY_X` 存成 `LARK_BRIDGE_X`；改了名的七个存成 `LARK_BRIDGE_MAIN_*`，如 `COVEY_BUTLER` 存成 `LARK_BRIDGE_MAIN_THREAD`），所以升级不改清单，回滚到旧 release 也读得到；`scripts/deploy_bridge.sh` 的环境比对把清单和终端里的名字都折成旧名再比（`env.canonical`），只改了名字不算变化。Manager 启动服务时所有写法都设进服务进程（清单里的值盖过 Manager 自己环境里的同名设置），所以旧 release 的任务进程也读得到。
- **Bridge 给子进程设的变量**（`COVEY_TASK_DIR`、`COVEY_BUTLER_STATE`、`COVEY_BUTLER_JOB`、`COVEY_RUNNER_PYTHONPATH` 等）所有写法都设（管家的两个另设 `COVEY_MAIN_*`、`LARK_BRIDGE_MAIN_*`）；找任务遗留进程（`core/task_processes.py`、内存看门狗）认两种。
- 启动时只对真有旧名的变量在 stderr 打印一行“`LARK_BRIDGE_X` is the old name of `COVEY_X` …”；改名之后才有的开关（`COVEY_PROJECT*`、`COVEY_PATROL*`、`COVEY_PROGRESS*`、`COVEY_TRACK_JUDGE*`、`COVEY_DEVICES*`、`COVEY_NODE*`、`COVEY_WEB*`，`core/env.py` 的 `BORN_NEW`）没有旧名，服务清单里存成的 `LARK_BRIDGE_X` 只是存放写法，读到也不提示。
- 下一个版本起清单才改存新名，旧名读取再保留至少一个版本。

## 新旧版本混跑

部署换的是 Bridge，已经在跑的任务进程用启动它的旧 release 跑完；回滚时反过来。所以新旧版本要互相认得：

- **`runs/<任务键>/`**：Bridge 和任务进程之间的协议目录，新旧 release 双向兼容；文件名和读写规则在 `core/run_dir.py`，跨版本检查见[测试 · 任务进程协议与跨版本检查](testing.md#任务进程协议与跨版本检查)。
- **`harnesses.json` 和旧命令行**：现网写着的选项都还能解析，删掉的功能留下它的选项名，值被忽略（如 `--claude-transport stream-json`，见[已下线的功能](legacy.md#已下线的功能)）。
- **旧记录**：Bridge 启动时和 `covey_maintenance.py --apply` 先跑一次旧记录迁移（`core/migrate.py`），做完记进状态目录的 `migrations.json`；读记录的代码因此不需要兼容分支。
- **已发出的卡片**：旧卡上的按钮 kind 点了不崩溃、不悄悄开工，只回一句它已失效或挪到了哪里（见[使用参考 · 已下线的功能](reference.md#已下线的功能)）。

兼容旧记录、旧任务进程和旧卡片的代码都带 `# compat(<提交>): …; retire when …` 注释。`scripts/compat_report.py --state <状态目录> --harness-config <Harness 配置>` 按这些注释数出本数据根里还有多少东西要它。它只读运行者自己的数据根，所以删掉一处兼容之前，要每个部署者各跑一次，那一项在所有部署里都是 0（“可删”）才能删；“不知道”不算 0。

## 旧安装

- **`.local-state` 目录**：代码目录下已有 `.local-state/<组件名>` 时，按组件解析的默认路径继续用它，优先于 `COVEY_DATA_HOME`；显式 `--state-dir` 仍然最优先，bootstrap 不看它。解析到它时 stderr 记一行 `warning: legacy .local-state path resolved: …`。请把它移到数据根下，后续版本不再读取。`python3 -B bin/covey_paths.py <组件名>` 只读显示解析结果，`--new` 忽略 `.local-state`。
- **单后端机器人的旧状态目录**（`codex-lark-bridge/`、`claude-lark-bridge/` 等）留在原位，统一机器人不读也不删。要继续旧工作，在统一机器人里新开话题交接。
- **同一应用升级为统一机器人**：先停掉旧服务，避免同一应用有两套消费者；再显式用原目录作 `--state-dir`。旧话题默认归属 Codex。换新应用时必须用新的状态目录和 owner。
- **没有自动迁移**：任务、检查点和后端原生记录里有绝对路径。要搬状态目录时先停服务和任务，保留备份和旧路径，验证后再切换。
- **旧 Skill**：`scripts/install_skills.sh` 发现仍装着旧名的 Skill（`lark-<名字>-bridge` 这类）或别的已停用 Skill 时给警告，不自动删除。
- **旧安装脚本**：`scripts/setup_harness_bridge.sh` 仍可用、不再推荐，见[手工安装](manual-install.md)。

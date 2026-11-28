# 已下线的功能与旧配置

本文写给升级老部署的人：以前的配置、旧话题和旧卡片现在会怎样，哪些变量和参数已经不读。新装的部署用不到这一篇。现在的配置见[配置](configuration.md)，改名 Covey 时保留的旧名、新旧版本混跑和老的安装形态见[改名与兼容](compat.md)。

兼容代码什么时候可以删（`scripts/compat_report.py`）见[改名与兼容 · 新旧版本混跑](compat.md#新旧版本混跑)。

## 已下线的功能

| 已下线的 | 旧配置或旧话题现在会怎样 |
| --- | --- |
| 已下架的 Harness（`core/roster.py` 的 `RETIRED`），以及名单外的任何 Harness ID | 旧话题里发消息或点卡片，只回“本话题使用的 <名字> 已下架，不能继续。…”，不执行；日志带 `outcome=retired_harness`。Bridge 启动时把它们没结束、也没有存活任务进程的任务标为中断并通知一次，存盘的排队任务丢弃。`harnesses.json` 里的这些键忽略。话题、任务记录、原生会话和旧状态目录留在原处，机器人不读也不删 |
| Pi（`pi`；GPT 模型用 Codex 跑） | 同上一行：Pi 的旧话题只回已下架。旧 `harnesses.json` 的 `enabled_harnesses` 里写着 `pi` 时跳过它、照常启动，`bridge.log` 记一行 `harness config enables retired Harnesses, left out: pi`，只写了它一个时按没写处理（启用全部）；`harnesses.pi` 的参数（`--pi-cmd` 等）只记一行 `harness config ignores Harnesses not in the roster`。网关配置（扩展 manifest 或 `harnesses.json` 的 `gateways`）的 `harnesses` 里写着 `pi` 照常解析、忽略它。设置卡里管家、新话题、独立复核或专项负责人选的是 Pi 时按没选处理（跟着启用了的走）。状态目录里的 `pi-sessions/` 和 `tmp/pi-home-*` 不再读写，可以删 |
| 已下线的模型供应商（`core/providers.py` 的 `RETIRED_PREFIXES`：几家 Claude 和 Codex 的供应商，以及下一行的 Claude 网关档位） | 启动卡、换配置卡和保存的上次选择都不再给出它们的模型。停在它们上面的话题发消息时回复“本话题的模型来自已下线的供应商「…」…请点任务卡上的“🔁 换配置”…”，换配置后照常继续。旧命令行和旧 `harnesses.json` 里它们的参数（`--traex-ppe-env`、`--traex-site`、`--codex-magpie-url`）还能解析，值被忽略，设了非空值时启动记一行 `no longer used`；它们的变量（如 `COVEY_MAGPIE_URL`）不再读，旧的模型目录缓存留在盘上、不再读 |
| Claude 网关档位（`--claude-profiles` 配置文件、包装命令、手工 pending/ready、`profile/<id>/<模型>` 选项） | Covey 只用本机自己装好并登录的 Claude Code 或 Codex。`profile/…` 模型不再列出，保存的上次选择、排队任务和旧话题里的 `profile/…` 不启动，回复同上一行。`--claude-profiles` 仍能解析，值被忽略，设了启动记一行 `no longer used`；配置文件留在盘上，不再读。要接自己的中转，改用扩展（见 architecture.md 扩展） |
| 已删除的 Claude 网关模型（形如 `<网关>/<模型>`、不在当前目录里） | 不再列出；停在它上面的话题在启动 Claude Code 之前失败，卡上提示换配置 |
| `--claude-transport`、`--codex-transport` | 只为旧命令行和旧 `harnesses.json` 能解析，只有一个值 |
| 后端参数的环境变量（`core/ignored_env.py` 的 `IGNORED_ENV`，扩展的 Harness 另有） | 不再读取，设了会在启动日志提示换成哪个参数；其中两个权限变量设了非默认值会让诊断和部署预检失败，bootstrap 拒绝注册。见下面[不再读取的变量](#不再读取的变量) |
| 自动加速（`COVEY_FERRY_AUTO_ACCELERATE`） | 不再读这个变量。单独发的“加速”“超频”和别的消息一样，按普通消息或补充处理 |
| `main-thread.json` 的 `desk_topics`（前台话题布局） | 读到 `true` 也按 `false` 处理，启动时记一行 warning，保存设置时原值留着。以前开的前台话题（登记在 `<state>/main-thread/desk/`）由启动时的迁移 `desk-retire` 冻结，不再接新消息 |
| 脱敏标签的“新话题默认开”（`redaction/config.json` 的 `default_on`） | 不再读：新话题默认开不开脱敏是设置卡「配置」页“新话题”一段的“脱敏”一行（`main-thread.json` 的 `topic_defaults.redaction`）。启动时的迁移 `redaction-default` 在它开着时把那一行打开一次；值留在 `config.json` 给旧版本。旧卡上点这个开关只说它挪到了哪里 |
| 脱敏词表里的替身词（`stand_in`） | 替身词一律自动生成：读时忽略旧词表的 `stand_in`，启动时的迁移 `redaction-stand-in` 把旧词表重写一遍；旧版本设的 `"anywhere": true` 照旧生效，不能新设 |
| 文字命令、斜杠命令 | 以 `/` 开头的消息和以前的命令词（“代答”“本周用量”“管理工作区”“换配置”“停用本群”等）当作普通任务或补充。只有配对口令 `/pair` 还认。应用里残留的原生斜杠命令要在开发者后台删除并发布 |
| 旧菜单键 `manage_workspaces`（仓库）、`redaction_dashboard`（脱敏）、`manage_groups`（群聊）（`proxy_settings` 现在又是正式的「代答」项） | 它们的页面在设置卡的页签里；应用菜单里的这三项由「飞书应用待补」的补齐一并去掉（`core/app_manifest.py` 的 `STALE_MENU_KEYS`），客户端缓存的旧菜单点它们，回一句“已移到「设置」卡的「X」页签”（`app/bot_menu.py`）。`main_thread_settings` 照旧能点，打开设置卡的配置页 |
| 独立的敏感词网页看板（`COVEY_DASHBOARD`、`COVEY_DASHBOARD_URL`，扩展钩子 `dashboard_entry` 和它的 `COVEY_DASHBOARD_ENTRY`） | 不再读这几个变量，清单里留着也照常启动；扩展的 manifest 还写着 `dashboard_entry` 钩子也照常加载（`core/extensions.py` 的 `RETIRED_HOOKS`），核心不再调它。状态目录里的 `redaction/dashboard-token` 和 `redaction/dashboard-entry.json` 不再用，可以删（`app/bot_menu.py` 还从后者读旧链接卡发去的私聊）。旧链接卡和设置卡「脱敏」标签上的“换口令”（`bridge_dashboard_rotate`）点了只说网页看板已经去掉；旧菜单键 `redaction_dashboard` 照旧打开「脱敏」标签 |
| 两卡式或 schema 3 之前的旧启动卡 | 点了只回“此启动卡已失效，请在主聊天重新发送消息开始任务。”，不改表单和等着的消息 |
| 管家的“直接开工”设置（`main-thread.json` 的 `start`）和“记住的工作区”（`trusted`），以及设置卡上它们的行和按钮（`bridge_main_start_row`、`bridge_main_start`、`bridge_main_untrust`）、网页设置页的 `start_note`、`trusted` 和 `untrust` 操作 | 管家开的话题一律直接开工（运行方式是独立 worktree 的除外，仍出启动卡），不再读这两个字段：旧文件里有也能读，不报错，设置卡下次保存时不再写回。旧卡上点这三个按钮，设置卡回“直接开工已下线…”并重画，不改设置；网页的 `untrust` 回 failed。管家不再问、也不再记“以后这类直接开工吗” |
| 以后这类按推荐走（`COVEY_STANDING_ANSWERS`）：问题卡下面的按钮和它那张卡（`bridge_question_standing`、`bridge_standing_save`、`bridge_standing_edit`、`bridge_standing_cancel`） | 不再读这个变量，清单里留着也不报错，`./install` 不再写它。旧卡上点这几个按钮只回一句“已下线”（`app/card_actions.py` 的 `MOVED_CARDS`），不记原则、不回答。以前记下的原则（`source.by` 为 `standing_answer`）照旧生效；`<state>/standing-forms/` 留在盘上，备份照旧跳过，可以手动删 |
| 任务卡「下载请求与结果」（`COVEY_TURN_EXPORT`） | 整块删掉，不再读这个变量，清单里留着也不报错。已经发出的卡上的旧按钮点了只回一句“📥 下载请求与结果已下线，这次点击没有打包任何东西。”，不崩、不开工；要看一轮的请求和回答，读 Harness 自己的会话文件。Harness 能力 `turn_export` 改名 `session_files`，只给子 Agent 恢复读会话文件用 |
| 可用性检查里的真实模型调用（`COVEY_HARNESS_PROBE`） | 不再读这个变量。可用性检查只看本机的命令和登录，不再对 Claude、Codex 各做一次真实调用；`core/harness_probe.py` 的探测留给 `./install` 和 `bin/covey_doctor.py` |
| 小模型判断走模型网关（`COVEY_JUDGE_GATEWAY`） | 不再读这个变量，走网关的那条路径删了。同时做几件事的分流、整理专项现在都经辅助模型，问它选的 Harness 自己的命令行，见[配置 · 辅助模型](configuration.md#辅助模型) |
| 菜单「看板」的开关（`COVEY_BOARD`） | 不再读这个变量，菜单「看板」每个部署都有，早晚汇总关了也有（应用菜单和斜杠命令都一直要这一项） |
| 专项汇报风格的开关（`COVEY_PROJECT_STYLE_KEEP`） | 不再是开关，也不读这个变量。负责人用 `project_style` 改过的汇报风格总会记进 `<state>/project-style.json`，新开的专项从它开始 |
| 我本来想找你的事（巡逻，`COVEY_PATROL` 和它的 10 个 `COVEY_PATROL_*` 数值） | 整块删掉：不再读这些变量，服务清单里留着也照常启动、不报错（部署预检 `dropped_config` 不列它们）；要清掉用 `bin/covey_service.py unset --state-dir <状态目录> COVEY_PATROL`。后台线程、小模型判断、汇总里的“我本来想找你的事”一节、维护里清 `patrol/` 的规则都没有了。状态目录里的 `patrol/` 不再读写，也不再被清理，可以手动删 |
| 同时做几件事里分流的待命进程（`COVEY_TRACK_JUDGE_WARM`、`COVEY_TRACK_JUDGE_HEDGE`） | 不再读这两个变量，清单里留着也照常启动。以前每条管家消息都先起两个 Claude Code、各用一次真实请求热身，只换来每问快约 1 秒；现在每问现起一个一次性进程，`COVEY_TRACK_JUDGE_MS` 的默认从 3000 放到 6000（Claude Code 冷启动一问约 3.4 秒） |
| 整理专项的实时归并（`COVEY_PROJECT_TIDY_AGE_DAYS`、`COVEY_PROJECT_TIDY_WORDS`，新专项建好后的扫一遍） | 不再读这两个变量。新话题、新专项该收进来的话题都等下一次定时或手动整理；`projects/tidy/state.json` 里旧版本排队的 `queue` 下次读话题要点时清掉，`topics/<话题>.json` 的 `fired`、`judged_*` 不再读；旧版本夜里扣下的提议卡早上照常发 |
| 「看看」、整理记忆里的模型那一步 | 按钮和功能都在，只是不再调辅助模型：看看回规则那一句和事实，整理记忆只按规则收起 |
| 晚间候选（晚间汇总找“你反复说的”进待审核）、Harness 记忆导入（夜里读各 Harness 的记忆文件进待审核、网页记忆页的「导入」）、网页记忆页的体检、“下一轮带什么”、名额条和五个页签 | 整块删掉。`<state>/memory-candidates.json`、`<state>/memory/imports.json` 原样留着，不再读写；待审核里旧版放进去的候选、导入项照旧能采纳或不要；网页「记忆」改成一页（[数据约定 v45](web.md#数据约定-v45记忆一页式)）；负责人记的规矩不再进待审核 |
| 早晚汇总的“你反复说的”“自动学习想记的”“Codex 和 Claude Code 自己记的”三节（`bridge_digest_remember`） | 汇总卡不再列这三节，只留“记忆：待审核 N 条”一行；候选和各 Harness 自己记的偏好都在设置卡「记忆」页签或网页记忆中心的待审核里。已发出的旧卡上点「记下」「不用」只回一句已下线，不记任何东西。晚间候选和整理记忆也不再调模型，旧 `memory-candidates.json`、`memory-tidy.json` 照常能读（里面的 `superseded`、新约定字段忽略） |
| 整理记忆的“新约定”和它的「确认长期」「不对」按钮（`bridge_digest_memory` 的 `confirm`、`wrong`） | 不再提炼新约定，汇总卡上只剩「撤销」。旧卡上点这两个按钮只回一句已下线，不改记忆；以前已经写进专项的“它总结的”约定留着，在待审核里处理 |
| 其他已下线卡片的按钮 | 见[已下线的功能](reference.md#已下线的功能) |

## 不再读取的变量

下面这些以前给后端参数设默认值（扩展的 Harness 另有，见扩展的 README），现在不再读取，后端用字面默认值（`--help` 里的那个）。参数改写在 Codex 的服务命令行或其余 Harness 的 `harnesses.json`：

- Codex：`LARK_BRIDGE_CODEX_{MODEL,REASONING_EFFORT,SANDBOX,CMD}`、`LARK_BRIDGE_TIMEOUT` → 服务命令行 `--model`、`--reasoning-effort`、`--sandbox`、`--codex-cmd`、`--codex-timeout`；
- Claude：`LARK_CLAUDE_BRIDGE_{MODEL,EFFORT,CLAUDE_CMD,TIMEOUT,SKIP_PERMISSIONS,LOGIN_SHELL}` → `--model`、`--effort`、`--claude-cmd`、`--claude-timeout`、`--no-skip-permissions`、`--claude-login-shell`；
- `LARK_DSH_BRIDGE_CMD` → `--dsh-cmd`；
- `LARK_PI_BRIDGE_CMD`：Pi 已下线，删掉就行（启动日志写 `env LARK_PI_BRIDGE_CMD is no longer read; unset it: Pi is retired`）。

服务清单会把终端里的它们存下来，所以老部署重启后还带着，只是不生效。Bridge 启动时对每个设了的变量在 `bridge.log` 记一行 `env <名字> is no longer read; put <参数> in …`（`core/ignored_env.py` 的 `IGNORED_ENV`）。在没有它们的终端里重新注册（[改设置后重新注册](deploy.md#改设置后重新注册)）就清掉了。

其中两个以前用来收紧权限，不再读取后会悄悄放宽，所以设了非默认值时直接拦下（`core/ignored_env.py` 的 `loosened`）：`LARK_BRIDGE_CODEX_SANDBOX` 不是 `danger-full-access`（Codex 会按 `danger-full-access` 跑），`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 是 `0`/`false`/`no`/`off`（Claude 会跳过授权）。这时 `bridge.log` 的那一行写明会怎样，`bin/covey_doctor.py` 把这个服务报为 `degraded`、带 `permission_env` 并非 0 退出，`scripts/deploy_bridge.sh --check-only` 的预检 `permission_env` 失败（清单和当前终端里的都查），`scripts/bootstrap_harness_bridge.sh` 拒绝注册。改法见[权限变量让诊断或部署失败](troubleshooting.md#权限变量让诊断或部署失败)。

非默认 Harness 总是用 Bridge 的 `--profile` 和 `--state-dir`，所以 `LARK_CLAUDE_BRIDGE_PROFILE` 等和非默认 Harness 的 `<前缀>_STATE_DIR` 设了也不起作用。

## 环境变量的旧名

旧名 `LARK_BRIDGE_<X>` 和 `MAIN_*` 写法照常生效，服务清单仍按旧名存，见[改名与兼容 · 环境变量的旧名](compat.md#环境变量的旧名)。

## 旧安装

`.local-state` 目录、单后端机器人的旧状态目录、旧 Skill 等见[改名与兼容 · 旧安装](compat.md#旧安装)。

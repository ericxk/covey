# Harness 说明

Harness 是执行任务的 Agent 程序。本篇按上手顺序写：有哪些、各能做什么 → 怎么装和登录 → 启动卡上的模型从哪来 → 每个 Harness 的行为和参数 → 模型网关 → 在设备上运行、执行权限 → 改 `harnesses.json` 和参数 → 适配器代码。飞书里怎么用见[使用指南](usage.md)，启动卡缺模型见[排障](troubleshooting.md#启动卡里缺少某个-harness-的模型)。

## 支持的 Harness

核心带四个：Codex、Claude Code、DeepSeek Harness（dsh）、Antigravity（`core/roster.py`；Pi 已下线，见[已下线的功能与旧配置](legacy.md)），[扩展](architecture.md#扩展)可以再加（见扩展自己的 README）。**供应商**是模型从哪里来（账号、网关或 API），各有自己的模型和思考预算（`core/providers.py` 的 `PROVIDERS`，`provider_of` 按模型 ID 前缀认）。一个 Harness 可以有几个供应商。

| Harness | 供应商（模型 ID 前缀） | 登录或密钥 |
| --- | --- | --- |
| Claude Code | 本机 Claude Code 的登录（无前缀，换配置卡写“Claude 自带”） | `claude` 自己的登录 |
| Codex | 自己的登录（无前缀）；配了模型网关时还有网关（`<网关前缀>`，如 `mygw/`） | Codex 的登录；网关的个人密钥 |
| DeepSeek Harness | 只有模型网关（无前缀）：没有给 dsh 用的网关就用不了 | 网关的个人密钥 |
| Antigravity | 自己的登录（无前缀，写“Antigravity 自带”） | agy 的 Google 账号登录 |

核心不带任何一家网关，网关由 `harnesses.json` 的 `gateways` 或扩展配置（[模型网关](#模型网关codex-与-dsh)）。同一个账号只列一次：Harness 自己的登录（Claude Code 的 `ANTHROPIC_BASE_URL` 加 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`，进程环境里没有就读用户级 `settings.json` 的 `env`；Codex 的 `config.toml` 里 `model_provider` 指的那一项的 `base_url` 和 `env_key`）和它的某个模型网关地址相同、密钥也相同时，两者是同一账号（`core/same_account.py`，密钥只比摘要）。这时模型选择里只有网关这一个供应商：自带登录里网关也有的模型不再单列，其余的列在网关名下；它们的模型 id 不变，照旧用自带登录跑，停在上面的话题不受影响（`app/launch_models.py` 的 `fold_same_account`）。扩展里的来源（经中转、包装命令跑，Covey 看不到它的地址）就是自带登录的账号时，由扩展在 `PROVIDERS` 里写 `"own_login": True` 说明，效果一样（[自己的模型来源走扩展](architecture.md#扩展)）。已下线的 Harness 和供应商（`RETIRED_PREFIXES`）、旧话题会怎样，见[已下线的功能与旧配置](legacy.md)。

| ID（启动卡名称） | 运行方式 | 原生问答 | 执行中补充 | 原生图片 | 分叉的会话 | 原生会话位置 |
| --- | --- | --- | --- | --- | --- | --- |
| `codex`（Codex） | 每个任务一个 `codex app-server` | 支持，含多选 | 支持 | 支持 | `thread/fork` | `$CODEX_HOME/sessions`，默认 `~/.codex/sessions` |
| `claude`（Claude） | 每轮一个双向 `claude -p`（stream-json） | 根任务单选、多选、自由文本 | 支持，可到“模型已读取” | 支持 | `--resume --fork-session` | `$CLAUDE_CONFIG_DIR/projects/<目录编码>`，默认 `~/.claude/projects/…` |
| `dsh`（DeepSeek Harness） | 每轮一次 `dsh --profile headless --json` | 无，经检查点提问 | 无 | 无 | 不支持 | `$DSH_HOME/sessions`，默认 `~/.dsh/sessions` |
| `agy`（Antigravity） | 每轮一个 `agy` headless（stream-json），补充时换一个续接的进程 | 无，经检查点提问 | 支持：等当前一步做完再停下续接，急的可打断命令 | 无（给路径，它自己读图） | 不支持 | `~/.gemini/antigravity-cli/conversations` |

- **原生问答**：Claude 的问题在原任务里等回答；Codex 必须回答的问题先结束本轮，回答排成续接任务。所有 Harness 都能经检查点提问（[回答模型的问题](usage.md#回答模型的问题)）。
- **原生图片**：不支持时只给本地路径。Claude 单张超过 5 MiB 或合计超出 8 MiB 帧时，放不下的只给路径（`app/runs.py` 的 `_fit_images`）。
- **分叉**：管家分叉出的话题（[开话题、转交、分叉](butler.md#开话题转交分叉)）复制原话题的原生会话；不支持或原话题正在执行时改用已完成的对话记录。**换配置**都开新会话。
- **只有 Claude Code 有的**：[回退一步](usage.md#回退一步)、本轮未全部完成后自动继续、轮前压缩。
- **管家、专项负责人和辅助模型**：管家和专项负责人跑在能接 Bridge 工具的 Harness 上，核心的四个都能：Claude Code、Codex、Antigravity，以及 DeepSeek Harness（自带的 `dsh-mcp-client`）（`HarnessCapabilities.bridge_tools`）；设置卡没指定时先用 Claude Code，没有就依次找 Codex、Antigravity，再找 DeepSeek Harness（`app/butler_harness.py`）。负责人可以和管家不同：设置卡“专项负责人”一段、专项卡的「换配置」（[专项 · 负责人用哪个 Harness](projects.md#负责人用哪个-harness)）。负责人只读：Claude Code 只给读和搜索的工具，Codex 跑只读轮（见[执行权限](#执行权限)），Antigravity 用本轮的拒绝规则，DeepSeek Harness 用 read-only 权限预设（没有可用沙箱的机器上 bash 整个不能用），见[专项 · 负责人只读](projects.md#负责人只读)。后台的一问一答经辅助模型，默认跟着管家换（`ask_once`；DeepSeek Harness 和扩展里没声明 `ask_once` 的 Harness 不能一问一答，不在候选里；[配置 · 辅助模型](configuration.md#辅助模型)）。扩展里的 Harness 没声明 `bridge_tools` 的不能当管家、负责人。
- **每个内置 Harness 都有的**：限流（429）和临时故障（过载、5xx、连接断开）后[自动重试](reference.md#自动重试)（`rate_limit_retry`、`fault_retry`）。各家怎么认出这两类写在下面各自的“失败”一行；登录失效、额度用完、模型不存在、请求被拒、安全拒绝和停止不重试。
- **交接摘要**（[换配置之后](reference.md#换配置之后)）：Claude Code、Codex 在原会话的分叉上写；Antigravity 和 dsh 的命令行只能续接会话（`--conversation`、`--session-id`），续接会把摘要这一轮留在原会话里，所以不写，新模型拿最近 3 轮原文。

## 安装和登录 Harness

`./install`（[安装](install.md)）的前提只有一条：机器上已装好并登录了 Claude Code、Codex 或 Antigravity 之一，不用每个都装。安装器和机器人都不替你安装、不替你登录；安装时对 Claude Code、Codex 和 Antigravity 各真实调用一次模型（`service/installer.py` 的 `PROBED`），没登录、网络不通会逐个说明，没调通的不启用，没装的合成一行。`PATH` 里找不到时再看 `~/.local/bin` 和 `~/.claude/local`，找到就把目录加进服务的 `PATH`（`locate`）。

- **Claude Code**：按[官方说明](https://docs.claude.com/en/docs/claude-code/setup)安装，在 `claude` 里登录；验证：`claude -p "ok"` 有回答。
- **Codex**：按[官方说明](https://github.com/openai/codex)安装，`codex login`；验证：`codex exec --skip-git-repo-check "ok"` 有回答（不在 git 仓库里不加这个参数会被拦下）。
- **要代理才能访问模型**：运行 `./install` 前先 `export HTTPS_PROXY=http://主机:端口`（探测像网络不通时安装器也会问）；探测通过后代理和飞书域名的 `NO_PROXY` 一起写进服务环境。不能带用户名密码。

CLI 登录和注册服务要用同一个系统账号、同一套环境变量。注册时终端里的 `PATH`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME`、代理变量和所有 `COVEY_*`（含旧名）写进服务清单，开机恢复后仍按它们运行；名字像凭据的不写入，见[服务清单里的环境变量](configuration.md#服务清单里的环境变量)。

### 进阶 Harness（dsh、Antigravity、扩展的）

这些要自己准备，不装也照常能用。Antigravity 装好登录后 `./install` 会探测并启用；dsh 装好、给它用的网关的密钥写进文件（权限 600）后，`./install` 不调用模型就启用（`set_up_harnesses`；密钥只在终端的环境变量里时服务拿不到，安装器会说写进哪个文件），已经装过的重跑一次 `./install` 就加上。扩展的 Harness 由扩展的安装钩子在 `./install` 第 3 步报告并启用，准备方法看扩展的 README。不用 `./install` 时按[手工加一个 Harness](#手工加一个-harness) 加进 `harnesses.json`。

| Harness | 准备什么 |
| --- | --- |
| dsh | `npm install -g @deepseek-ai/dsh`，再配一个给 dsh 用的 Anthropic Messages 网关（[最小网关示例](#最小网关示例)），没有网关用不了 |
| Antigravity | 官方脚本 `https://antigravity.google/cli/install.sh` 安装（装到 `~/.local/bin/agy`），在运行机器人的账号下跑一次 `agy` 选 Google OAuth 登录。见 [Antigravity](#antigravity) |

准备好后在每个 CLI 里用自己的账号发一次真实请求：模型出现在列表里不代表当前账号能调用。可执行文件不在 PATH 里时，用 `--<名字>-cmd` 写绝对路径（[常用参数](#常用参数)）。

## 选模型

启动卡的模型每项写作“Harness · 供应商 · 模型名”，Harness 自己的供应商不写（“Claude · Opus 5.5”“Codex · MyGW · GPT-6 Astra”）。选模型就选定了 Harness 和供应商，之后可以[换配置](usage.md#换配置)。没有上次选择时预选 Codex 的 `--model`（不在目录里时选第一项）；`models_first` 可以把常用的排到前面（[`--harness-config` 文件](#--harness-config-文件)）。

### 模型目录

启动卡只列出各 Harness 实时读到的模型（`app/launch_models.py`）：后台每 5 分钟刷新，15 分钟内没刷新成功的 Harness 从启动卡上消失（只是超时没回应时上次的目录最多再用 1 小时，`KEEP_UNANSWERED`），点卡片时不做慢查询。目录里标为弃用、移除或隐藏的不列；失败时 `bridge.log` 记 `launch catalog unavailable harness=<ID> error=<错误类名> detail=<原因>`。

| Harness | 目录来源 |
| --- | --- |
| Codex | `~/.codex/models_cache.json` 里 `visibility` 为 `list` 的模型，加各网关 `/v1/models` 里 `openai-responses` 协议的模型（`<网关前缀><模型>`）；都读不到时 Codex 不出现 |
| Claude | 一次 `claude` 双向初始化返回的模型和档位；接托管网关时含网关 `/v1/models` 的模型 |
| dsh | 给 dsh 用的网关 `/v1/models` 里 `anthropic-messages`（或 `anthropic`）协议的模型 |
| Antigravity | `agy models` 列出的（当前 Google 账号能用的）。末尾是档位的 ID（`gemini-4-neon-high`）合成一个模型、档位做思考预算，没有档位的单列；没登录时不出现 |

另外不列：扩展按自己规则隐藏的模型和档位；Claude 的旧版本（`claude-sonnet-`、`claude-haiku-`、`claude-opus-4-` 开头的，带不带网关前缀都算；别名 `sonnet`、`haiku` 照常列出）；网关目录里的 `auto`、`auto-max`。已固定在这些模型上的话题照常运行。

## Codex

- **准备**：装 `codex` 并用本人账号登录；任务开始前只检查可执行文件。
- **运行方式**：每个任务一个 `codex app-server`，经 stdio 通信。命令、文件修改、MCP 工具、计划和子 Agent 都进执行过程（命令没有写说明的字段，桥接规则要它在命令第一行写一行 `# 这一步做什么`，执行过程显示这句；没写时显示去掉开头样板以后真正在跑的命令）；模型、推理强度、工作区和权限每轮传入；`agents.max_threads=4`。桥接规则在新会话首轮的用户输入开头，续接轮附提醒和约定（[一次运行](architecture.md#一次运行)）。
- **原生提问**：不需要停下的显示在任务卡上，其他工作继续；必须先回答的让本轮先结束，回答后在同一会话继续。
- **开发者指令**：线程只认创建时的 `developerInstructions`（续接、fork、`turn/start` 都改不了，压缩后保留），所以那里放桥接规则（系统级，见[架构 · 续接轮的规则](architecture.md#续接轮的规则)）和线程不变的提问说明（`backends/codex/appserver.py` 的 `thread_instructions`：负责人的，或通用的）；按这一轮 `TOOL_LIMIT` 变的说法（`codex_instructions`：协作咨询、群成员只读）放在这一轮输入的末尾（`turn_instructions`）。各轮的说法：普通轮（要问的写检查点 `questions`，request_user_input 只在不答就做不下去时用）；协作咨询轮只留子 Agent 一句（不问、不写检查点）；负责人轮不写检查点，要主人定的事用负责人的 Bridge 工具 `project_ask` 问（`core/butler_tool_specs.py` 的 `LEAD_ASK_TOOL`），不用 request_user_input；群成员只读轮不写检查点。
- **工作区只读工具**：每个会话挂本任务专用的 `bridge_workspace` MCP 服务，可读工作区、附件目录和换配置的 `handoff/`，拒绝 `..`、符号链接和二进制；实际权限只由 `--sandbox` 决定。
- **本轮结果**：thread 和 turn 双重绑定，只用当前 turn 的完整 items（不完整时按 ID 回读），取 `final_answer` 阶段的 agentMessage。子 Agent 恢复要读的会话文件在 rollout 里（能力 `session_files`）。
- **执行指标**：token 取 `thread/tokenUsage/updated` 的累计减本轮基线（`CodexTurnUsage`），子 Agent 会话各自减基线；生成时间从 `turn/start` 计，扣除工具和提问区间（重试也算在内，token/s 会偏低）；TTFT `codex_item`、`codex_delta`。
- **管家的工具**：管家跑在 Codex 上时（设置卡「配置」页的“管家”一段的 Harness），它那一轮的线程另挂 Bridge 工具服务 `bridge`（`backends/codex/appserver.py` 的 `bridge_servers`），工具等待上限 `tool_timeout_sec` 150 秒，比服务自己的 120 秒长。`approvalPolicy: never` 下调用不弹审批（0.167.2 实测）。
- **失败**（`backends/codex/turn.py` 的 `codex_failure_code`，按 `codexErrorInfo` 分，不看原文）：`usageLimitExceeded`、`rateLimitExceeded` 或 429 记 `rate_limited`，原文说 quota exceeded、billing、要升级套餐的记 `quota_exhausted`；`serverOverloaded`、`internalServerError`、`flexUnavailable`，以及连接失败或中断（`httpConnectionFailed`、`responseStream*`、`responseTooManyFailedAttempts`）没有状态码或是 500、502、503、504、529 的记 `server_fault`（原文说的是账号的事，如 could not authenticate、sign in again 记 `authentication_error`，insufficient_quota、额度用完记 `quota_exhausted`：`core/limit_reset.py` 的 `account_fault`）；带别的状态码的连接错误还是 `transport_error`；`unauthorized` 和 401、403 记 `authentication_error`（网关每日额度的 403 记 `quota_exhausted`）；安全审查拒绝（`cyberPolicy`、`bioPolicy`、`misalignmentPolicyViolation`，或 OpenAI 固定的那句 “Invalid prompt: your prompt was flagged as potentially violating our usage policy”，codex-cli 0.167 把它报成 `other`）记 `policy_refusal`，不自动重试；`modelNotFound` 记 `model_unavailable`；请求错误、上下文超长和其余的记 `root_failed`。失败卡片的技术详情写“Codex 原话：<错误类型>: <原文>”（`said_words`，所有错误都写，没归类的也一样），运行日志（`runner.log` 的 `backend stderr`）也记一份；没有 turn 错误就结束的（app-server 退出、协议错误），写 app-server stderr 里最后一条报错行（只留 ERROR、panicked、error: 这类行，其余的不读），没有就写退出码。
- **交接摘要**（`backends/codex/handoff.py`）：在原会话的 `thread/fork`（`ephemeral`，不写 rollout，原会话不动）上请原模型写，单独起一个 app-server，`CODEX_HOME` 用协作同款的私有目录（只链登录和会话，不带主人的 MCP、hook、规则、插件、记忆），只读沙箱、不审批，关掉两种 shell（`shell_tool`、`unified_exec`）、子 Agent、网页搜索、浏览器和看图等工具，不挂 Bridge 工具和工作区只读工具，服务端发来的审批或提问一律拒绝；最多 180 秒，超时或停止时关掉 app-server 并停它的进程组。网关模型同样经一个本轮的中继。
- **原生压缩**：Bridge 不改 Codex 的自动压缩（不传 `model_auto_compact_token_limit`），模型目录没给 `auto_compact_token_limit` 时由 Codex 自己定（0.172.0 的 27.2 万窗口模型，任务卡报的窗口是它的 95%，即 25.84 万；轮末上下文实测最高 24.48 万，约为 27.2 万的 90%）。
- **参数**写在 bootstrap 命令上，不进 `harnesses.json`（[常用参数](#常用参数)）；网关模型见[模型网关](#模型网关codex-与-dsh)。

## Claude Code

- **准备**：装 `claude` 并登录。任务开始前跑 `claude auth status --json`，未登录不执行；通过后 5 分钟内沿用结果（`app/availability.py` 的 `LOGIN_REUSE`）。环境要靠交互式 shell 才齐全时加 `--claude-login-shell`（任务、模型目录和回退的命令经 `bash -ic` 启动；登录检查不经这层，`--claude-cmd` 仍要在 `PATH` 上或写绝对路径）。
- **运行方式**：每轮一个双向 `claude -p --input-format stream-json`。桥接规则每次启动都用 `--append-system-prompt` 追加在 Claude Code 自己的系统提示后面（会话沿用第一次的，压缩后按当次的重新生成）；之前开的、系统提示里没有规则的老会话，续接轮照旧在用户消息里附格式提醒、工作区和检查点约定。见[架构 · 续接轮的规则](architecture.md#续接轮的规则)。
- **问答**：只回答本轮根任务发出、和已观察到的工具调用一致的 `AskUserQuestion`（1 至 4 题、每题 2 至 4 个选项）；保密问题、子 Agent 的提问和其他权限请求一律拒绝。待答问题属于原任务进程，进程结束后失效。
- **补充**：Claude 进入会话后才能接收，之前先保存；送达未确认的不自动重发。图片格式不支持时本轮失败。
- **压缩**：`--claude-auto-compact-window TOKENS` 给子进程设 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`（一轮中途）；不设时子进程的环境原样传下去，由 Claude Code 按模型自己定压缩窗口（1M 模型的会话实测在 96.7 万～97.5 万 token 处压缩，2.1.x），CLI 自己也建议保持 auto；设得比这低，一轮中途会更早压缩，细节丢得更早。轮间压缩在续接的一轮开始前做：Bridge 要求（`compact_first`，如管家的阈值），或设了 `--claude-compact-between-turns TOKENS` 且上一轮结束时上下文达到它；用单独的 `claude -p --resume <会话>` 发 `/compact`，会话 ID 不变，token 算进本轮；失败或 20 分钟没完成按原上下文继续。
- **回退一步**（[用法](usage.md#回退一步)、[机制](architecture.md#回退一步)）：子进程带 `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`；去掉一轮用 `--resume-session-at`，改回文件用 `--rewind-files`。
- **会话保留天数**（`core/claude_retention.py`）：Claude Code 按它自己的 `cleanupPeriodDays`（`settings.json`，缺省 30 天，要正整数）删掉整个配置目录里更早的会话文件，更早的话题就接不上、回退一步也读不到。诊断页和 `./install` 读 `$CLAUDE_CONFIG_DIR/settings.json`（没设就 `~/.claude/settings.json`；项目级和托管的设置不读），缺省或少于约 90 天时提示加一行 `"cleanupPeriodDays": 3650`；Covey 只提示，不改你的 Claude 配置。
- **交接摘要**（`backends/claude/handoff.py`）：话题从有会话的 Claude 绑定换走后，新绑定的第一轮先用原模型在原目录 `--resume <旧会话> --fork-session`，请它写一份约 1500 字以内的交接摘要（最多等 180 秒，不带跳过权限和 Bridge 工具，分叉的会话用完删掉）。哪些情况不做见[使用参考 · 换配置之后](reference.md#换配置之后)和[架构](architecture.md#话题中途换配置)。
- **子 Agent 守则与 hook**（`backends/claude/hooks.py`，`COVEY_CLAUDE_HOOKS`，默认开）：每轮的 initialize 请求注册 SDK hook 回调，由 `backends/claude/control.py` 应答 `hook_callback`。子 Agent（Explore 除外，它有自己的提示词）开工时 `SubagentStart` 给它四句守则：替主 Agent 干活、不对主人说话也不发消息和文件；最终回复写清做了什么、结果、没做完的、产物绝对路径；要主人拍板的不可逆的事不猜，用能原样转给主人的问题收尾；之后可能被带着答案叫回来。有 Bridge 工具的轮次（管家、负责人）注册 `PreToolUse`：子 Agent 调 `mcp__bridge__*` 被拒，理由是“写进最终回复，主 Agent 会转达”；主线程自己调照常。管家这一轮（主人没说“你直接做”）另注册 `PostToolUse`：主线程调满 6 次非 Bridge 工具时提醒一次交给话题（`app/butler.py` 的 `tool_nudge`；能力 `nudges`，别的 Harness 由 Bridge 数工具步骤、作为补充送进去，见 [管家](butler.md)）。`COVEY_CLAUDE_HOOKS=0` 不注册，和原来一样。
- **子 Agent 的模型**（`backends/claude/subagents.py`，设置卡「配置」页“更多”里的“子 Agent”）：默认“轻量”，子进程带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，并用 `--agents` 把内置 Explore 换成只读、跑在 Haiku 上的同名子 Agent；“跟主模型”两样都不加。Claude Code 接的网关认不出 `sonnet`、`haiku` 别名时选“跟主模型”。
- **模型目录**：一次双向初始化，不提交 prompt。档位只取模型声明的 `low` 到 `max`，声明了 `xhigh` 的另有 `ultracode`。子进程带 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`，经 `ANTHROPIC_BASE_URL` 接托管网关时还列出网关的模型（如 `claude/claude-opus-5`）；某次一个都没列出时沿用 24 小时内的 `<state>/native-models/claude-gateway.json`。已删除的网关模型的话题在启动前失败，提示换配置。
- **本轮结果与收尾**：只取当前调用的 JSONL，result 文本等于最后一条完整根消息才算数。最终 result 后关输入，约 10 秒后停止进程组（还有补充没被接收时不关）；后台命令或子 Agent 结束时 Claude Code 会再起一轮通知，机器人等它们全部结束，只受 `--claude-timeout` 约束，这一轮的回复是各轮回复按顺序连起来（紧挨着的相同段落只留一段，`backends/claude/completion.py` 的 `final_texts`）。有结果后 CLI 非零退出时按部分结果投递（会话封住）；因停止或超时结束、已有完整回复的照常发出并加说明。原生错误详情在 `<状态目录>/claude-diagnostics/<run-id>.json`，不要外传。
- **命令先排队等席位**（只对用 `--claude-cmd` 指向包装脚本的部署有用，默认不生效）：包装脚本要先排队等网关空位时，CLI 还没启动，initialize 没人应答。脚本在 stderr 至少每 30 秒写一行 `covey queue: {"ahead": 1, "running": 3, "cap": 3, "limit": 3600, "name": "<网关名>"}`（字段都可省；`limit` 是它最多等几秒，到了自己退出），轮到时写 `covey queue: {"admitted": true}` 再启动 Claude Code。Bridge 据此一直等它的初始化回答（不超过 `limit`，最多 4 小时，此后再等 30 秒），任务卡状态行写“排队”；停写超过 30 秒、过了 `limit`，或还没轮到就自己退出，都记 `queue_timeout`。不写这种行的命令照旧只等 30 秒（`initialize_timeout`）。这些行不进失败日志的 stderr 摘录。
- **限流与额度**：429 记 `rate_limited`（会自动重试），529、500、502、503、504、连接重置、流空闲超时记 `server_fault`（临时故障，会自动重试；但原文说的是账号的事时不算：网关把登录失效、额度用完也回成 5xx，Claude Code 照样说“服务端问题，通常是暂时的”，这时按原文记 `authentication_error` 或 `quota_exhausted`，不自动重试，`core/limit_reset.py` 的 `account_fault`），402 记 `quota_exhausted`，451 记 `model_blocked`（后两种提示换配置）。明确的官方拒绝或降级会停止当前任务，见[官方拒绝与降级停止](policy-stop.md)。失败时 result 行里的 API 状态和错误原文（`errors`，或出错的 `result`）写进卡片技术详情（`completion.claude_words`）；没有 result 行就结束的（网关断开、会话找不到、进程退出），写 stderr 里说明原因的最后一行（如 `claude gateway: RemoteDisconnected`、`No conversation found with session ID`，`process.said_words`），stderr 也没有时写传输失败的原因、阶段和退出码。
- **执行指标**：输出 token 是每条消息最大的 `output_tokens` 之和；缓存取 `cache_read_input_tokens`、`cache_creation_input_tokens`；子任务按 `parent_tool_use_id` 算，退出后读 `subagents/agent-*.jsonl` 补值；上下文取 result 的 `modelUsage`；生成时间扣除 tool_use 到 tool_result；TTFT `claude_delta`。

## Antigravity

Antigravity 是 Google 的 Agent 开发平台，这里用它的命令行 `agy`（替代了 Gemini CLI），跑当前 Google 账号能用的模型。

- **安装**：`curl -fsSL https://antigravity.google/cli/install.sh | bash`，二进制在 `~/.local/bin/agy`，之后它在后台自己升级；按 1.5.13 验证过。
- **登录**：在运行机器人的系统账号下执行一次 `agy`，选 “Google OAuth”；没有浏览器时它打印授权链接，在自己的浏览器里登录后把授权码贴回终端。没有 D-Bus 时令牌存在 `~/.gemini/antigravity-cli/antigravity-oauth-token`，不进机器人的记录。一台机器一个登录，agy 没有切账号的参数，只按 `HOME` 找登录。也可以用 Gemini API 密钥或 Google Cloud 项目登录（[官方说明](https://antigravity.google/docs/install)）。没登录时启动卡上没有它，已有话题的一轮以“还没有登录”失败。
- **运行方式**：每轮一个 `agy --output-format stream-json --input-format stream-json --disable-slash-commands --dangerously-skip-permissions [--conversation <会话>] [--model <模型>] [--effort <档位>]`，正文作为一行 JSON 从 stdin 传入后关闭，跑完这一轮就退出。续接用 `--conversation`，会话 ID 和 `init` 里的不一致时按协议错误失败。
- **本轮结果**：最后一行 `result` 的 `status` 为 `SUCCESS`、零退出码。正文取最后一个工具、子 Agent 或系统步骤之后模型写的字（`result.response` 把这一轮的字全拼在一起，取出的不是它的结尾时退回整个 `response`）；子 Agent 的步骤不算本轮正文。
- **事件与用量**：文字增量是正文和进展说明，工具步骤按名字显示；工具输出和思考不上卡片。每一步写模型自己说的这一步做什么：agy 的 stream-json 不带调用的 `toolSummary`（实测 1.5.14：`run_command` 只流出 `CommandLine`），Bridge 按步骤号和参数从本机 agy 的会话库（`$HOME/.gemini/antigravity-cli/conversations/<会话>.db`）只读查出来写在前面（`backends/agy/bridge.py` 的 `tool_summary`）；查不到（设备上的回合、agy 换了存法）时用命令第一行的 `# 注释`，再没有就显示去掉开头样板以后真正在跑的命令。`schedule`（定时等待）写回来做什么和多久以后；`edit_file`、`write_file`、`update_steps` 按修改文件、写入文件、更新计划显示。用量取每个 `agent_response` 步骤的 `usage`（`input_tokens` 不含缓存读取，`output_tokens` 含思考），没有时才用 `result.usage`，而且只在这个进程新开会话时用：`result.usage` 是整段会话累计的，续用的会话里拿它会把以前各轮都算进这一轮（实测：管家一轮开头几秒被补充打断、第一个请求还没结束，这一轮记成了会话累计的 1.6 亿输入）；每一步的三项之和是下一步开始时的上下文（`context_tokens`，管家的压缩阈值读它）。不报 `context_window`：`agy models`、stream 的 init/step/result 行和本机会话库里都没有模型窗口。dsh 同样报 `context_tokens`（最近一步的输入含缓存加输出），网关模型目录没有窗口字段，不报 `context_window`。
- **模型和档位**：启动卡的预算就是 `--effort`；模型目录（`launch-models/agy.json`）说没有这个档位时不启动，没有档位的模型不传 `--effort`。
- **失败**（`backends/agy/bridge.py` 的 `error_code`）：`result.error` 像登录失败记 `authentication_error`；带安全拦截字样（SAFETY、PROHIBITED_CONTENT、违反政策等）的记 `policy_refusal`（不自动重试）；模型不是这个账号的或已下线（`invalid model selection`、`unknown model key`、`model not found`，结果里没说、只在 stderr 说的也算）记 `model_unavailable`；像额度用完、又没说什么时候恢复的记 `quota_exhausted`；像 429、限流，或额度说了恢复时间（reset after、Resets in、per minute、until 某时）的记 `rate_limited`，恢复时间写进 `limit.reset_at`，自动重试等到那时；恢复在 6 小时以后的（如 `Individual quota reached … Resets in 157h…`）记 `quota_exhausted`，不自动重试，没配 [agy 多账号](quota.md#agy-多账号) 时还把这台机器的登录在这个模型分组（Gemini、Claude/GPT）上挡到恢复（`accounts.hold`）：期间新的一轮开工前就停，说清几点恢复；最后一次失败 10 分钟后放一轮过去再看。话题绑的模型不在已存的模型列表（`launch-models/agy.json`）里时，开工前就停，记 `model_unavailable`（`AntigravityBackend.start_refusal`，经 `app/runs.py`；列表为空、用 agy 默认模型、在设备上跑时不查）。`UNAVAILABLE`、`INTERNAL`、`DEADLINE_EXCEEDED`、50x、过载、连接重置或断开、“network issue” 记 `server_fault`；其余 `root_failed`。点了停止后 agy 以错误收尾的，限流和故障都按停止记。模型不可用、额度几点恢复写进卡片标题（`failure_words`）；不论哪类错误，agy 的原话（结果里的 error，没有时取 stderr 里 `AGY_ERROR` 的 short_error 或 `error:` 行，`said_words`）都写进卡片技术详情；stderr 最后几行（去掉登录链接、隐藏凭据）进运行日志。
- **补充**是“停下再接着做”（`AgyControl`）：agy 运行中不收新消息，收下补充即“后端已接收”，先暂存在 `AgyControl.pending`。有工具或子 agent 步骤在跑时不打断（适配器在步骤开始时调 `step_started`，带上 shell 工具的命令，步骤 `DONE` 时调 `step_done`），等最后一个在跑的步骤结束才给 agy 发 SIGINT；没有步骤在跑时立即发。补充和步骤结束同时到：判断都在 `self.lock` 下，只中断一次，不丢、不重复送。agy 以 `interrupted` 结束并保留已做的部分；它还在跑的命令先 TERM、5 秒后 KILL；然后同一会话起新进程，消息是补充，这时算“模型已读取”。急的补充（20 秒）和回执上的「打断它，马上读」调 `AgyControl.stop_command(note)`（本机的 `capabilities.command_stop`，设备上关）：在跑步骤的命令命中 `core/steer.py` 的 `keeps_running`（推送、部署）时返回 `kept` 不打断，没有命令在跑、正在收尾、只在等子 agent 时返回 `reason`，否则走同一条 SIGINT，`note` 排在补充前面。两种续接文案：被打断的写“停下来的命令没有跑完”，步骤之间送达的写“上一步做完后停下来读这条补充”；两种和换环境续接（`SWITCH_RESUMED`）都要求“最后的回复要写完整的结论”，因为任务的回复只取最后一个进程写的（`StreamRows.reply`）。暂存期间主人点停止或任务结束：`take()` 取走的补充没有发出 `steer_read`，`end_unread_steers` 把它记成“模型读到之前就结束”（有回执的显示“重新排队执行”，管家的在停止时不重排，见 `requeue_lost`）。代价：一步跑很久（全量测试）时补充要等它结束，出口是急的补充和「打断」。
- **换个环境接着做**（核心自带的 [agy 多账号](quota.md#agy-多账号)，配了两个号才开；没开时是扩展的 `agy_turn` 钩子，[架构 · 扩展](architecture.md#扩展)，两个不能同时开）：没有时一轮和以前一样。有的话，它给本机每一轮第一个 agy 进程的环境；某个进程以额度用完、限流或没登录失败时再问它一次，它给回另一个环境就在那里接着同一个对话：agy 已经收下这一轮的话（有 `user_input` 那一步）时发一句“接着完成上面的要求”，没收下就把原话再发一遍；执行过程里多一条换号的说明；一轮最多换 3 次。用途是一台机器上的几个 Google 账号：一个账号一个 `HOME`，对话是本机文件，另一个账号能接着跑（1.5.14 实测）。账号表、额度查询（`agy -p /usage --output-format json`，不耗 token）和挑哪个账号在 `backends/agy/accounts.py`；一个号都不剩时这一轮按“额度用完”失败，写最早几点恢复（`settle`）。
- **规则在系统级**：agy 没有追加系统提示的参数，但每轮都把 `~/.gemini/GEMINI.md`、`~/.gemini/config/GEMINI.md` 读进系统指令（压缩后也读）。本机的每一轮都在私有主目录里跑（`backends/agy/home.py` 的 `PrivateHome`），那里的 `GEMINI.md` 是你自己的（有的话）加桥接规则，超过 23000 字节的部分放进 `config/GEMINI.md`；真实的 `~/.gemini` 不写。设备上的轮次照旧把规则放在消息里。
- **管家的工具**：agy 只从 `~/.gemini/config/mcp_config.json`（和插件）读 MCP 服务，`--settings` 不收 `mcpServers`。管家跑在 agy 上时，那一轮的 `HOME` 换成 `<state>/tmp/agy-home-*`（`backends/agy/home.py`；同一个话题每一轮用同一个目录名，按话题和真实主目录算出来，旁边的 `.lock` 保证同一时刻只有一轮在用，被占着时这一轮另起一个随机名。agy 把 `HOME` 写进提示词开头（App Data Directory 和每个 skill 的路径），以前每轮随机名，提示词从那里起就和上一轮不同，每轮第一个请求一点缓存都用不上）：真实主目录的每一项、`.gemini` 和它的 `config` 里的每一项都是指回原处的符号链接，只有 `mcp_config.json` 是这一轮自己的：主人原有的服务（文件是纯 JSON 时；带注释的读不了，这一轮就不带，`bridge.log` 记一行）加上 Bridge 的 `bridge`。开跑前问一次服务的 `tools/list`，这一轮要用的工具标 `tools.<名字>.eager`：agy 把它们当模型自己的工具给出去；没标的 agy 只在提示里列名字，模型要先读工具说明文件，再经它的 `call_mcp_tool` 调用。全部标上不行：agy 把一轮的 skills 和 MCP 工具算进同一份预算，超了就从最大的开始整个丢掉，声明和提示里的清单都不留，也不报（1.5.16 实测：装了很多 skills 时，管家开着专项的 21 个工具里最大的 `topic_start`（5 KB 多）没了，管家只好拿 `topic_fork` 分叉别的话题来开新的事）。所以 Bridge 按顺序挑：先是交给话题的（`topic_start`、`topic_send`、`topic_fork`、`project_send`，负责人的 `project_topic_start`、`project_topic_send`、`project_ask`），再是其余常用的，原则、知识条目、历史搜索、看板优先、设置卡、整理专项，负责人的 `project_goal`、`project_remember`、`project_style`、`project_topic_stop`、`project_memory_revise`（`core/butler_tool_specs.py` 的 `SECONDARY`）排最后，加起来不超过 15000 字节（描述加参数 schema，放不下的那个工具跳过、不占预算，后面小的照样预载；`COVEY_AGY_EAGER_BYTES` 可改，0 是全部只列名字），其余只列名字。一轮结束后 Bridge 只读查 agy 会话库里最近一次请求的 MCP 清单（`backends/agy/bridge.py` 的 `left_out`），Bridge 的工具有不在里面的就在日志里记一条警告，写明是哪几个；看到它就把 `COVEY_AGY_EAGER_BYTES` 调小，或少装些 skills。`timeoutSeconds` 150。登录、会话、日志都经链接写回原处；这一轮在主目录顶层新建的文件随私有目录一起删掉。agy 会在 `~/.gemini/antigravity-cli/mcp/bridge/` 留工具说明的缓存。
- **管家的压缩**（capability `compaction`，`backends/agy/compaction.py`）：管家跑在 agy 上、一轮结束时上下文过了设置卡的压缩阈值，Bridge 等 180 秒（主人的快速追问先走），话题还空着就在同一会话上跑 agy 自己的 `/compact`：一个不带 `--disable-slash-commands`、其余参数和环境（模型、档位、`HOME`、Bridge 工具）都跟这一轮一样的进程，正文只有 `/compact`。agy 发一个 `checkpoint` 类型的请求写摘要，流里是一个 `step_type` 为 `checkpoint` 的步骤（带这次请求的用量）和空的 `result`，会话 ID 不变（1.5.16 实测：7 万上下文的摘要约 30 秒，请求的 96% 读缓存）。参数和环境一样，是为了这次请求的开头和上一轮相同、在服务端缓存还热时（Neon High 约 5 分钟，XHigh 不到 1 分钟）只按缓存计。会话太短时 agy 不压缩，把 `/compact` 当消息交给模型回答，没有 `checkpoint` 就不算压缩。`/compact` 后面的说明文字 agy 也会在压完后当消息让模型回答，所以不带说明。没赶上空闲压缩的（主人很快又发了消息），上下文到阈值 1.5 倍时下一轮开头先压，任务卡执行过程里有这一步。
- 没有原生问答、原生图片（只给路径，它用自带的 `read_file` 打开）、原生分叉（所以换配置时不写交接摘要）、回退一步；在设备上的差别见[在设备上运行](#在设备上运行)。

## 模型网关（Codex 与 dsh）

模型网关是“OpenAI Responses / Anthropic Messages 兼容”的上游，由配置描述，可以有多个；核心不带任何一家的地址。配置来自扩展 manifest 的 `GATEWAYS`（`core/extensions.py`）和 `harnesses.json` 顶层的 `gateways`（自己部署时配自己的网关，例子见[最小网关示例](#最小网关示例)）。每个网关一个名字，字段：扩展带的网关（名字、地址、密钥怎么拿）写在扩展自己的 README 里。

- 必填 `base_url`（上游地址）、`key_env`（密钥所在的环境变量）。给 Codex 用的要有 `prefix`（模型 ID 前缀，如 `mygw/gpt-6`）；给 dsh 用的模型 ID 不带前缀，dsh 只用第一个声明它的网关。
- `display` 是卡上的名字；`access` 是换配置卡上“登录或密钥在哪”的提示；`harnesses` 是哪些 Harness 用它（`codex`、`dsh` 的子集，默认都用；旧配置里写着的 `pi` 照常解析、忽略）；`quota_pattern` 是 403 响应里表示额度用完的正则；`busy_hint` 是繁忙失败卡的末句。`headers` 是请求时另带的头；可选 `base_url_env`、`key_file_env`、`header_envs`（头名到环境变量）覆盖地址、密钥文件和头的值。
- 密钥先读 `key_env` 指的环境变量，再读 `key_file`（相对路径在 `$XDG_CONFIG_HOME` 或 `~/.config` 下，默认 `harness-bridge/<名字>-api-key`，必须本人所有、权限 `600`）。
- 没有网关时 Codex 只剩自己账号的模型，dsh 没有模型；一个网关的密钥没配时只是它的模型不出现。目录缓存在状态目录的 `native-models/<名字>.json`，繁忙日志在 `<名字>/relay.log`。Codex 的网关请求经本地中继，429 时在中继里等一阵再试（`backends/gateway/relay.py`）。

### 每日额度用完

有 `quota_pattern` 的网关回 403、原文对得上、又写了 `Resets at <UTC 时间>` 时，Covey 把这份额度记成用完，到那个时间为止（`core/quota_hold.py`，记录在状态目录的 `<名字>/quota-hold.json`）：

- **中继**：这个 403 和之后的请求，中继都回给 Codex 一个 429 `insufficient_quota`，不再往上游发。Codex 对它立刻结束这一轮；原样转 403 的话，Codex 要重试 10 次、白等约 205 秒（codex-cli 0.172.0，本机假上游实测）。Codex 请求时要 gzip，中继原样转给上游，所以 403 的正文常是压缩的：中继按 `Content-Encoding`（gzip、deflate）解开再认（`relay.py` 的 `plain`），转回给 Codex 的仍是原来的字节。
- **开工前**：话题的模型走这份额度的，新一轮不启动，直接停住（`app/runs.py`，`quota_exhausted`）。
- **卡上的话**（`failure_detail`，`core/failure.py` 照它写原因行）：“<显示名> 的每日额度用完了，北京时间 HH:MM 恢复；<显示名> 的所有模型共用这一份额度，换 `<前缀>` 下的别的模型也没用，可以换到 …”。不是今天恢复的带日期。
- **负责人派活**：`project_topic_send` 的目标话题走这份额度时不发，回一句几点恢复、先换配置（`app/project_tools.py` 的 `spent_quota`）。
- **提前恢复**：上次确认用完 10 分钟（`RECHECK`）以后，下一个请求照常发上游查一次，其余的仍在本地回绝。它成功了（比如主人申请了临时额度）就不再拦，又是 403 就再等 10 分钟。
- 原文没写恢复时间的，这一轮照样立刻失败，但不记录，下一轮照常发。

别的线路用同一把密钥时（比如另一条线路里配的同一个网关），在 `harnesses.json` 顶层写 `gateway_quotas`，按网关名：

```json
{"gateway_quotas": {"mygw": {
   "shared": ["mygw-personal/"],
   "instead": ["", "other/", "Claude"]}}}
```

- `shared`：和这个网关共用额度的 Codex 模型前缀（以 `/` 结尾）。这些模型的一轮因这份额度失败时也记下来，额度用完期间一样开工前就停、派活时一样提醒。
- `instead`：卡上建议换到的，按顺序列。以 `/` 结尾的是 Codex 模型前缀，名字从 Codex 模型目录里这个前缀的第一个模型读（“… · 名字”），目录里没有这个前缀的模型就不列；`""` 是 Codex 账号的模型（不带前缀）；其余原样写（例如另一个 Harness）。不写就是“可以换到不走 <显示名> 的模型或 Harness”。
- 只列确认过额度独立的线路。写了不存在的网关名不报错（可能是之后才启用的扩展的）。

### 最小网关示例

Bridge 从 `GET <base_url>/v1/models` 读模型目录（带 `Authorization: Bearer <密钥>` 和 `headers`），返回 `{"data": [...]}`，每行至少有 `id` 和 `protocol`：`openai-responses` 的给 Codex，`anthropic-messages`（或 `anthropic`）的给 dsh；没有 `protocol` 的行不列，只按 OpenAI 格式列模型的网关要补上这一项。可选 `display_name`、`supported_reasoning_levels`、`default_reasoning_level`；`status` 为 `deprecated`、`removed` 或带 `hidden` 的不列。Codex 的请求发到 `<base_url>/v1/responses`，dsh 的发到 Anthropic Messages 接口。

一个同时给 Codex 和 dsh 用的网关，写进 `harnesses.json` 顶层：

```json
{"enabled_harnesses": ["codex", "claude", "dsh"],
 "gateways": {"mygw": {
   "display": "MyGW", "prefix": "mygw/", "base_url": "https://gateway.example.com",
   "key_env": "MYGW_API_KEY", "harnesses": ["codex", "dsh"],
   "access": "密钥在 MyGW 的个人设置页创建"}},
 "harnesses": {"dsh": {"args": ["--model", "<目录里一个 anthropic-messages 模型的 ID>"]}}}
```

```bash
mkdir -p ~/.config/harness-bridge
printf '%s\n' '<你的密钥>' > ~/.config/harness-bridge/mygw-api-key   # 默认文件名是 <网关名>-api-key
chmod 600 ~/.config/harness-bridge/mygw-api-key
python3 -B bin/covey_service.py restart --state-dir "$STATE_DIR"
```

重启后启动卡上 Codex 多出 `mygw/<模型>`（卡上写“Codex · MyGW · 模型名”），dsh 出现网关里 `anthropic-messages` 的模型。没出现时看 `bridge.log` 的 `launch catalog unavailable`：“个人密钥未配置”是密钥文件不在或权限不对，“模型目录不可用”是地址、密钥或网络。

### dsh（DeepSeek Harness）

dsh 是 DeepSeek 开源的 Agent Harness（npm 包 `@deepseek-ai/dsh`，预览版），运行模型网关的 Anthropic Messages 模型：没有声明给 dsh 用的网关时启动卡上没有它，已有话题的一轮报“没有配置给 dsh 用的模型网关”。

- **安装**：`npm install -g @deepseek-ai/dsh`，按 `0.2.1-rc.2` 验证，升级后跑[真实环境验证](testing.md#真实环境验证)。任务开始前只检查可执行文件和网关密钥。`--model` 的默认值是某一家网关的模型名，你的网关目录里没有它时改成有的，否则启动卡预选不到。
- **运行方式**：每轮一个 `dsh --profile headless --patch <补丁> --json [--session-id <会话>] -`，正文从 stdin 传。补丁是本轮私有的临时文件：声明以网关名命名的 provider、设模型和档位（`output_config.effort`）、关闭起标题的请求。环境 `DSH_PERMISSION_MODE=danger-full-access`、`DSH_TELEMETRY_MODE=DISABLED`。
- **规则在系统级**：补丁里另有 `system-prompt` 一项，`personaSuffix` 是桥接规则（规则里的 `{{` 拆开，否则 dsh 当成模板变量报错），`personaPrefix` 写回 headless 默认那句（这一项整份替换）；dsh 每轮按补丁重拼系统提示，压缩后也在。
- **本轮结果**：首行 `session` 一致、有 `turn_start`、最后 `turn_end` 为 `completed`、零退出码，正文取无截断的 `final`。事件每步完成才发出，没有 TTFT。续接用 `--session-id`，只能在创建会话时的工作目录。
- **失败**（`backends/dsh/bridge.py` 的 `headless_outcome`，按 `turn_end` 里 dsh 自己的错误码）：`RATE_LIMIT` 记 `rate_limited`，`SERVER`、`TIMEOUT`、`TRANSPORT`、`STREAM_CLOSED` 记 `server_fault`；`QUOTA`、`ACCOUNT_QUOTA`（dsh 说的余额或额度用完）和网关每日额度的 403 记 `quota_exhausted`；`AUTH`、`MISSING_CREDENTIAL`、`INVALID_CREDENTIAL`、`ACCOUNT_SIGN_IN_REQUIRED`、`ACCOUNT_TOKEN_INVALID` 记 `authentication_error`；`CONTENT_REFUSED`（安全拒绝）、`UNKNOWN_MODEL`、`INVALID_REQUEST` 和其余的记 `root_failed`。点了停止后以限流或故障收尾的按停止记。
- 没有原生问答、执行中补充、原生图片、原生分叉（所以换配置时不写交接摘要）；网页搜索要 `DEEPSEEK_API_KEY`。

## 设备通道

和下一节的“在设备上运行”是两回事：这里 Harness 留在 Bridge 这台机器上、从主目录启动，只是从自己的 shell 调 `covey device`（[配置 · 设备通道](configuration.md#设备通道随多设备开)）去别的设备上跑命令、取放文件、起长任务。开关同多设备（`COVEY_DEVICES=1` 加 `devices.json`）；设备上不用装 Harness、不用登录。因为 Harness 没离开 Hub，回退、压缩、分叉、Bridge 工具和只读轮的护栏都照常；设备命令的输出就是普通的命令输出。护栏（名单、只读设备、只读轮、凭据、审计、收割）写在 `core/device_access.py` 一处，三家走到这里是同一套规则（[架构 · 设备通道](architecture.md#设备通道)）。

| Harness | 怎么调 | 只读的轮次 |
| --- | --- | --- |
| Claude Code | Bash 里跑 `covey device …` | 负责人、群成员只读的轮只有 Read、Grep、Glob，没有 Bash，用不了设备通道；别的轮由通道自己按只读设备拦 |
| Codex | `exec_command` 里跑 `covey device …`（默认 `danger-full-access`） | 只读轮（负责人、群成员只读）在只读沙箱里：沙箱不让连 ssh 主连接的套接字、也解析不了主机名，运行目录只读写不了审计，所以这些轮次里 Codex 用不了设备通道：`covey device list` 还能列名单，别的子命令报设备没有答复（退出 4）。只读轮里的设备工具（沙箱外的工作区读取器里加只读的 `ls`、`stat`、`get`）在后面的版本补上 |
| Antigravity（`agy`） | `run_command` 里跑 `covey device …` | Covey 现行参数（`--dangerously-skip-permissions`，不传 `--mode`、`--sandbox`，权限模式 `always-proceed`）下 `run_command` 能出网，能用已有的 ssh 主连接，也能新开连接；负责人、群成员只读的轮拒绝所有 shell 命令（`command(*)`），用不了设备通道 |

- **怎么告诉模型**：本机话题的主目录段（系统规则，话题内不变）多一句固定的用法，和其余主目录规矩一起给到每家 Harness；设备名单在 `repos.md` 末尾按需读，或 `covey device list`。名单增删不改系统规则，Prompt 缓存不受影响。外部话题（`local: false`）不给这一句。
- **改设备上的文件**：三家都是 `covey device get` 到本话题产物目录、用自己的编辑工具改、再 `covey device put` 回去（`put` 核对取回时的 sha256）；Antigravity 的文件工具默认只碰工作区，产物目录在本机话题的可写范围里，不用放宽 `allowNonWorkspaceAccess`。
- **超过一次调用的命令**：用 `covey device job start`，不要让 `run` 等几十分钟：Harness 自己的命令超时会杀掉 `covey device`，设备上的命令则一直跑到 `--timeout`，一轮结束时才由 Bridge 按标记收割。

## 在设备上运行

开了多设备（`COVEY_DEVICES=1`，[配置](configuration.md#多设备开发中默认关)）后，工作区写成 `别名:路径` 的话题跑在那台设备上。能上设备的 Harness 由 `core/roster.py` 的 `devices` 列定：核心里是 Claude Code、Codex 和 Antigravity（设备的 `harnesses` 只认这几个，不写时是 `claude`、`codex`）。结构和不变量见[架构 · 设备](architecture.md#设备多设备默认关)。

| Harness | 在设备上 | 原因或差别 |
| --- | --- | --- |
| Claude Code、Codex | 能 | 见下面几条 |
| Antigravity（`agy`） | 能 | 用设备自己的 `agy` 和 Google 登录（在设备上运行一次 `agy` 登录；设备端程序看 `~/.gemini/antigravity-cli/antigravity-oauth-token` 在不在，不在就按没登录拒绝这一轮，自检也说没登录，不用 API key 登录）；执行中不能补充：补充要给 agy 发 SIGINT 再收拾它留在本机进程树里的命令，设备端程序只转发 TERM、KILL，所以任务卡上没有补充，话题里的新消息排成下一轮；stderr 留在设备上；启动卡的模型是 Bridge 本机 `agy models` 列的，设备登的账号没有所选模型时 agy 自己报错 |
| dsh | 不能 | 只用模型网关，要把网关的个人密钥放进进程环境（设备端拿不到，也不该送过去），路由补丁和 `DSH_HOME` 都是本机路径 |

- **设备上要有的**：Harness 的命令、登录、会话文件、模型目录、代理和证书。Bridge 不替设备登录，也不转发令牌。设备上的环境从空开始，只带设备档案里白名单内的代理和证书变量，再加 `devices.json` 的 `env`；`--claude-cmd`、`--codex-cmd`、`--agy-cmd` 在设备上换成自检时记下的路径，登录 shell 写设备的 `login_shell`。
- **启动前**：新加或改了设备先跑自检 `bin/covey_devices.py check <别名>`（[运维](operations.md#设备自检)），它在设备上对每个声明的 Harness 真跑一次最小请求（`core/harness_probe.py` 的 `command_for`；没有最小请求的 Harness 只查程序在不在，Antigravity 再查登录文件，结论里写明）；每轮开始前只读最近的自检和设备最近的答复，没登录当场拒绝。
- **和本机的差别**：没有回退、压缩和分叉；不取 git diff 快照。补充（Antigravity 除外）、原生问答和图片照常，消息里的文件先放到设备的 `.covey/incoming/`；产物和 `show_images` 从设备取回再上传。启动卡列的模型仍是 Bridge 本机的目录。
- **断线**：设备端小程序守着 Agent，Bridge 断线时照常跑；超过耐心值才按中断处理，恢复后先停旧进程再续接。

## 执行权限

默认面向个人开发机：Harness 以运行机器人的账号执行本机命令，能读写它能访问的所有文件。用自己的开发账号，不要把状态目录复制给同事；设备上的进程对那台设备有同样的权力。

| Harness | 默认 | 收紧方式 |
| --- | --- | --- |
| Codex | sandbox `danger-full-access`，审批策略 `never` | bootstrap 加 `--sandbox workspace-write` 或 `--sandbox read-only` |
| Claude | 传 `--dangerously-skip-permissions` | `harnesses.json` 写 `--no-skip-permissions` |
| dsh | `DSH_PERMISSION_MODE=danger-full-access`，审批 `never` | 没有开关：headless 没人能批准请求 |
| Antigravity | 传 `--dangerously-skip-permissions` | `harnesses.json` 写 `--no-skip-permissions`：要审批的操作在 headless 下被跳过 |

- 权限每轮按服务的当前配置传入，重启后已有话题的下一轮也按新设置。收紧后不会请你确认，无人值守的任务可能因权限不足失败，先用小任务验证（[真实环境验证](testing.md#真实环境验证)）。
- Codex `workspace-write`、`read-only` 下命令可能无法联网；`workspace-write` 下 `checkpoints/` 也可写；在设备上时可写根目录加上设备上的任务目录和检查点镜像目录。
- **主目录搜索垫片**（所有本机 Harness，W2-A）：主目录里放着各个仓库和大数据目录，不限深度地递归搜整个主目录可能耗尽内存。除了 Claude Code 的 `home-search` 钩子（只管 Claude Code 的 Bash、Grep、Glob 调用），Covey 在 `<数据根>/bin` 还放了同名的命令垫片：`rg`、`find`、`fd`、`du`、`tree`，以及只在带递归参数时才检查的 `grep`（`-r`、`-R`、`--recursive`、`-d recurse`、`--directories=recurse`）和 `ls`（`-R`、`--recursive`）。每个本机 Harness 进程（Claude Code、Codex、Antigravity、dsh、扩展的）的 `PATH` 最前都是这个目录（`core/home.py` 的 `env`），它们执行的 shell 命令都经垫片；设备上的轮次没有 `COVEY_HOME`，也不加这个目录，垫片不生效。判定和 `home-search` 同一套（`backends/claude/tool_guard.py` 的 `--mode shim`，复用 `searcher_targets`、`find_targets`、`covers_home`）：搜索的目标（没写路径就是当前目录）解析后是主目录或它的上级、又没有深度限制，就往 stderr 写一行中文说明、退出码 2，不执行；`rg <词> <仓库>`、`cd <仓库> && rg`、`find . -maxdepth 2`、`tree -L 2` 照常跑。管道或文件重定向喂给 `rg <词>` 的输入（标准输入是管道、文件或套接字）是在搜输入，不算搜目录。垫片自己出错（解释器起不来、release 的 `bin/` 没了、判定抛异常、`COVEY_HOME` 没设）一律原样执行真程序；真程序是 `PATH` 里去掉垫片目录（含软链接、重复出现）后的同名命令，所以 PATH 里的 `rg` 限额包装照样生效。垫片每次先在 sh 里用路径文本判断：当前目录不在主目录或它的上级，且参数里的路径要么是相对路径、要么在主目录里面（不含 `..`、`~`、`$`），就直接执行真程序，只多几毫秒；其余才起一次 Python（有字节码缓存时约 +35 ms，没有时约 +65 ms）。写在 `ensure_shim`：安装和每次部署（`force`）写成指向当前 release 的 `bin/`，不是 Covey 写的同名文件（没有“Covey 的搜索垫片”标记）不覆盖。**看不到的**：程序自己在里面递归的（Python 的 `os.walk`、`Path.rglob`、`glob('**')`，Node 的递归读目录，`git grep`）垫片拦不住（Claude Code 的钩子能看到命令行里内联的 Python），指向主目录的软链接目录也只在第二步（起 Python 时）才解开；绝对路径调用真程序（`/usr/bin/find`）绕过垫片。`rg` 垫片保留（没有降级）：本机实测常见路径（当前目录在仓库里）中位数多 5–8 ms，低于定的 10 ms 阈值；只有当前目录是主目录或它的上级时才多 35–65 ms，那一次本来就可能是在拦事故。
- Codex 的只读轮（只读群里群成员的一轮 `read`、专项负责人的 `lead`，[架构 · 一次运行](architecture.md#一次运行)）：app-server 关掉 apps（ChatGPT 应用的工具）、plugins、memories、hooks（`backends/codex/consult.py` 的 `READING_FLAGS`），提问和网页搜索照旧。`lead` 的 Bridge 工具服务设 `default_tools_approval_mode = "approve"`：只读沙箱里 MCP 工具默认要批准，`approvalPolicy: never` 下就全被拒（0.171.0 实测“MCP tool call requires approval”）；只批 `bridge`，主人自己的 MCP 服务照旧要批准、被拒。
  - `read` 和 `lead` 续接话题原来的线程时，在私有 `CODEX_HOME`（`<state>/tmp/codex-read-*`，`ReadingHome`）里跑：真实 home 里除 `rules/`、`hooks*`、`plugins/`、`memories*` 外的条目都链接过去，`config.toml` 也照旧链接。执行规则里 `decision="allow"` 的命令本来能在沙箱外执行，在这里也出不了沙箱。
  - `read` 和 `lead` 也可能是开线程的那一轮。在私有 home 里开的线程记在那个 home 下，home 删掉后就续接不到（`tests/test_collab_c8.py` 实测），所以开线程的那一轮留在真实 home 里。**执行规则里 allow 的命令这时仍在沙箱外执行**，Bridge 用的 `CODEX_HOME` 里别留宽泛的 allow 规则（如 `prefix_rule(pattern=["npm", "install", "-g"], decision="allow")`）。在设备上跑时用的是设备自己的 home，只有开关这一层。
- Antigravity 的负责人轮（`lead`）以及群成员只读、协作咨询轮（`read`、`consult`，在 agy 上本来就不启动）都用 `READ_ONLY_SETTINGS`：拒上面五个写文件工具、`command(*)` 和 `read_url(*)`。实测同上：`read` 轮的 `edit`、`write`、命令都被拒。
- 以前收紧权限的 `LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 不再读取，设了非默认值时诊断和部署预检失败，见[权限变量让诊断或部署失败](troubleshooting.md#权限变量让诊断或部署失败)。

## 配置 Harness 参数

### 手工加一个 Harness

以 dsh 为例，Antigravity 同样做（不用网关）：

1. **装好并登录**这个 Harness 的 CLI（[进阶 Harness](#进阶-harnessdshantigravity扩展的)），在终端里真实跑通一次。
2. **改 `harnesses.json`**：在数据根下（`./install` 装的是 `<仓库目录名>-data/harnesses.json`，`python3 -B bin/covey_paths.py` 打印数据根）。在 `enabled_harnesses` 里加上它的 ID，需要时写 `harnesses.<ID>.args`：

   ```json
   {"enabled_harnesses": ["codex", "claude", "dsh"],
    "harnesses": {"dsh": {"args": ["--dsh-cmd", "/absolute/bin/dsh"]}}}
   ```

   dsh 还要加 `gateways`，见[最小网关示例](#最小网关示例)；Antigravity 不用。
3. **重启 Bridge**：`python3 -B bin/covey_service.py restart --state-dir "$STATE_DIR"`（`STATE_DIR` 见[排障 · 先找到数据根和 APP_ID](troubleshooting.md#先找到数据根和-app_id)）。重跑 `./install` 不会让它生效：文件路径没变，安装器只说“配置没变”。
4. **确认**：几分钟内启动卡上出现它的模型（目录每 5 分钟刷新）。没出现看 `bridge.log` 的 `launch catalog unavailable harness=<ID> … detail=<原因>`，见[排障](troubleshooting.md#启动卡里缺少某个-harness-的模型)。

以后重跑 `./install` 时，你加的保留（按 Codex、Claude、dsh、Antigravity 的固定顺序重排，扩展的排在它声明的位置；第一项是新话题的默认 Harness），文件里别的键也保留。

### `--harness-config` 文件

除 Codex 外，各 Harness 的启动参数写在 `harnesses.json`。`./install` 只写其中的 `enabled_harnesses`（第一次写调通的；重跑时保留已有的，探测没过的只提示、不删），别的键原样保留，并把文件经 `--harness-config` 交给 bootstrap。手工注册（不用 `./install` 时）同样传（[注册与启动服务](operations.md#注册与启动服务)），文件内容的写法见[手工加一个 Harness](#手工加一个-harness)和[常用参数](#常用参数)：

```bash
./scripts/bootstrap_harness_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" \
  --harness-config /absolute/harnesses.json
```

| 键 | 含义 |
| --- | --- |
| `harnesses.<ID>.args` | 按命令行逐项书写的字符串数组。不是字符串数组或参数名写错时机器人启动失败。`codex` 键不读 `args` |
| `harnesses.<ID>.models_first` | 可选：这个 Harness 的模型先列哪几个（模型 ID；多个预设合成一项的写其中任一预设的 ID），目录里没有的跳过，最近选过的仍在最前。`codex` 键只读这一项 |
| `enabled_harnesses` | 可选：只启用这些，第一项是新话题的默认 Harness；省略时启用全部、默认 Codex。必须非空、不重复、只用支持的 ID；已下线的（如 `pi`）跳过，启动日志记一行 `harness config enables retired Harnesses, left out`，只剩已下线的时按省略处理 |

- 不在名单里（含已下架）的 Harness 键只记一行 `harness config ignores Harnesses not in the roster`，然后忽略。`--state-dir`、`--workdir`、`--profile` 由机器人统一传入，写了也不生效。文件里不写凭据。
- 服务和每个任务进程启动时按路径重新读，文件放持久位置、一次写完整；重启后生效。
- 用了 `enabled_harnesses` 时已有话题保留原 Harness，原后端被禁用后不改由其他后端执行。
- 同一个文件还放 `gateways`（[模型网关](#模型网关codex-与-dsh)）、`gateway_quotas`（[每日额度用完](#每日额度用完)）和 `spaces`（[配置 · 空间](configuration.md#空间spaces)）。

### 常用参数

Codex 的参数写在 bootstrap 命令上（`setup_harness_bridge.sh` 不接受），其余写进 `harnesses.json` 的 `args`。

| Harness | 参数（默认值） |
| --- | --- |
| Codex（bootstrap） | `--sandbox read-only\|workspace-write\|danger-full-access`（`danger-full-access`）；`--codex-cmd`（`codex`）；`--codex-timeout`（`0`）；`--model`（`gpt-6-astra`）；`--reasoning-effort`（`max`） |
| Claude | `--claude-cmd`（`claude`）；`--claude-timeout`（`0`）；`--model`（`claude-fable-5`）；`--effort`（`xhigh`）；`--no-skip-permissions`；`--claude-login-shell`；`--claude-auto-compact-window`、`--claude-compact-between-turns`（都不设置） |
| dsh | `--dsh-cmd`（`dsh`）；`--dsh-timeout`（`0`）；`--model`（`ali-deepseek-v4.1-flash`，要改成你的网关目录里有的）；`--reasoning-effort`（`high`） |
| Antigravity | `--agy-cmd`（`agy`）；`--agy-timeout`（`0`）；`--model`（空：agy 自己的默认）；`--reasoning-effort`（空：模型的默认）；`--no-skip-permissions` |

- 超时单位是秒，`0` 或负数不限时长。根任务给出最终回复后还要等子任务、待处理消息和进程清理收尾，设了超时时不超过任务的截止时间（`core/completion.py` 的 `CompletionBudget`），不改变 Harness 自身的模型请求重试。预算参数不影响启动卡的预选。
- 兼容的旧选项（现网 `harnesses.json` 仍能解析）：`--claude-transport stream-json`、`--codex-transport app-server`（只接受这一个值）；`--traex-ppe-env`、`--traex-site`（隐藏，值被忽略）。
- 扩展的 Harness 可以另有读环境变量的参数（见扩展的 README），命令行和 `--harness-config` 优先。以前给参数设默认值的变量不再读取（`core/ignored_env.py` 的 `IGNORED_ENV`，设了启动时各记一行），见[不再读取的变量](legacy.md#不再读取的变量)。

## 适配器结构

给改代码的人。后端契约（`core/harness.py` 的 `HarnessSpec`、`BackendRuntime`、`HarnessCapabilities`）和怎么加后端见[架构与开发说明](architecture.md)；后端不导入 `app/`、`feishu/`、`cards/` 和别的后端（`backends/gateway` 除外），见[硬约束](architecture.md#硬约束)。每个后端的 `bridge.py` 是 Harness 规格、参数和后端类，`sessions.py` 是原生会话位置，其余模块开头的 docstring 写着它管什么。

| 后端 | 代码位置 |
| --- | --- |
| Claude Code（`backends/claude/`） | `runtime.py` 一轮的收发与收尾（`run_stream`、空闲压缩 `compact_only`）；`process.py` CLI 进程和 JSONL 传输（`ClaudeProcess`、`TRANSPORT_FAILURES`）；`control.py` 输入、补充、问答和权限请求（`ClaudeControl`）；`argv.py` 启动参数和环境（`stream_argv`、`turn_env`）；`events.py` 事件（`ClaudeEventAdapter`）；`completion.py` 结果边界；`usage.py` 用量和 TTFT；`models.py` 目录行；`compaction.py`、`rewind.py` 压缩、回退；`policy.py` 官方拒绝 |
| Codex（`backends/codex/`） | `appserver.py` 一轮的开会话、起 turn、收尾（`run_appserver`）；`rpc.py` App Server 进程与 JSON-RPC（`AppServer`、`RpcError`）；`turn.py` 读一个 turn 到结束（`read_turn`、`close_turn`）；`events.py` 事件（`CodexEventAdapter`）；`usage.py` 用量；`gateway.py` 按会话分的 provider；`workspace_reader.py` 只读 MCP |
| Antigravity（`backends/agy/`） | `bridge.py` 参数、进程、事件（`AntigravityEventAdapter`）、结果（`stream_outcome`）、模型目录、换号（`extension_turn`、`Homes`）；`accounts.py` 多账号、`account_homes.py` 账号目录和 `agy-as`；`home.py` 管家的私有主目录；`judge.py` 辅助模型的一问一答 |
| dsh（`backends/dsh/`） | 只有 `bridge.py`：进程、补丁、事件（`DshEventAdapter`）、结果 |
| 模型网关（`backends/gateway/`，配置在 `core/gateways.py`） | `connection.py` 密钥、目录、请求头、`BUSY_WINDOW`；`relay.py` 中继；`catalog.py` 目录行和繁忙提示；每日额度用完记在 `core/quota_hold.py` |

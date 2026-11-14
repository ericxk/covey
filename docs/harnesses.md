# Harness 说明

本篇写给要选后端或给后端配参数的人：每个 Harness 和它的模型供应商是什么、差别在哪、怎么配置、有哪些限制。先看[支持的 Harness](#支持的-harness)的总表，改参数看[配置 Harness 参数](#配置-harness-参数)，改适配器代码看[适配器结构](#适配器结构)。安装步骤见[安装和登录 Harness](#安装和登录-harness)，飞书里怎么用见[使用指南](usage.md)。

## 支持的 Harness

Harness 是执行任务的 Agent 程序，核心带四个：Codex、Claude Code、DeepSeek Harness、Pi（`core/roster.py`），[扩展](architecture.md#扩展)可以再加（公司部署的见 `extensions/covey_internal/README.md`）。供应商是模型从哪里来（账号、网关或 API），各有自己的模型和思考预算（`core/providers.py` 的 `PROVIDERS`，`provider_of` 按模型 ID 前缀认供应商）。一个 Harness 可以有几个供应商，供应商不单独做成 Harness。

| Harness | 供应商（模型 ID 前缀） | 登录或密钥 |
| --- | --- | --- |
| Claude Code | 本机 Claude Code 自己的登录（无前缀，换配置卡写“Claude 自带”） | 本机 Claude Code 自己的登录 |
| Codex | 自己的登录（无前缀）、Acme（`acme/`） | Codex 的登录；Acme 个人密钥 |
| DeepSeek Harness | 只有 Acme（无前缀） | Acme 个人密钥 |
| Pi | Codex（无前缀，换配置卡写“Codex 自带”）、Acme（`acme/`） | Codex 的 ChatGPT 登录（只借用访问令牌）；Acme 个人密钥 |

启动卡的模型每项写作“Harness · 供应商 · 模型名”，Harness 自己的供应商不写（“Claude · Opus 5.5”“Codex · Acme · GPT-6 Astra”），Pi 两个供应商都写出。选模型就选定了 Harness 和供应商，之后可以[换配置](usage.md#换配置)。

已下线的 Harness 和供应商（`RETIRED_PREFIXES`）、旧话题会怎样，见[已下架项与旧配置](configuration.md#已下架项与旧配置)。

| ID（启动卡名称） | 运行方式 | 原生问答 | 执行中补充 | 原生图片 | 分叉的会话 | 原生会话位置 |
| --- | --- | --- | --- | --- | --- | --- |
| `codex`（Codex） | 每个任务一个 `codex app-server` | 支持，含多选 | 支持 | 支持 | `thread/fork` | `$CODEX_HOME/sessions`，默认 `~/.codex/sessions` |
| `claude`（Claude） | 每轮一个双向 `claude -p`（stream-json） | 根任务单选、多选、自由文本 | 支持，可到“模型已读取” | 支持 | `--resume --fork-session` | `$CLAUDE_CONFIG_DIR/projects/<目录编码>`，默认 `~/.claude/projects/…` |
| `dsh`（DeepSeek Harness） | 每轮一次 `dsh --profile headless --json` | 无，经检查点提问 | 无 | 无 | 不支持 | `$DSH_HOME/sessions`，默认 `~/.dsh/sessions` |
| `pi`（Pi） | 每轮一个 `pi --mode rpc` | 无，经检查点提问 | 支持，可到“模型已读取” | 支持 | `--fork <会话文件>` | `<状态目录>/pi-sessions/<时间>_<会话 ID>.jsonl` |

- **原生问答**：Claude 的问题在原任务里等回答；Codex 必须回答的问题先结束本轮，回答排成续接任务。所有 Harness 都能经检查点提问（[回答模型的问题](usage.md#回答模型的问题)）。
- **原生图片**：不支持时只给本地路径。Claude、Pi 单张超过 5 MiB，或 Claude 合计超出 8 MiB 帧时，放不下的只给路径（`app/runs.py` 的 `_fit_images`）。
- **分叉**：主线程分叉出的话题（[开话题、转交、分叉](main-thread.md#开话题转交分叉)）复制原话题的原生会话；不支持或原话题正在执行时改用已完成的对话记录。
- **换配置**除只改脱敏外都开新会话；**回退一步**只有 Claude Code 支持。

### 模型目录

启动卡只列出各 Harness 实时读到的模型（`app/launch_models.py`）。

- 后台每 5 分钟刷新。15 分钟内没刷新成功的 Harness 从启动卡上消失；只是超时没回应时上次的目录最多再用 1 小时（`KEEP_UNANSWERED`）。点卡片时不做慢查询。
- 目录里标为弃用、移除或隐藏的模型不列出。失败时 `bridge.log` 记 `launch catalog unavailable harness=<ID> error=<错误类名> detail=<原因>`。

| Harness | 目录来源 |
| --- | --- |
| Codex | `~/.codex/models_cache.json` 里 `visibility` 为 `list` 的模型，加 Acme `/v1/models` 里 `openai-responses` 协议的模型（`acme/<模型>`）；都读不到时 Codex 不出现 |
| Claude | 一次 `claude` 双向初始化返回的模型和声明的档位；接托管网关时含网关 `/v1/models` 的模型 |
| dsh | Acme `/v1/models` 里 `anthropic-messages` 协议（2026-11 起目录写作 `anthropic`，同样算）的模型 |
| Pi | 和 Codex 相同的两处来源，只取 `gpt` 开头的模型，去掉 `ultra` 档 |

不列出：扩展按自己的目录规则隐藏的模型和档位；Claude 的旧版本（`claude-sonnet-`、`claude-haiku-`、`claude-opus-4-` 开头的，带不带网关前缀都算；别名 `sonnet`、`haiku` 即 Sonnet 5.5、Haiku 5.5，2026-11-10 起列出）；Acme 的 `auto`、`auto-max`。已固定在这些模型上的话题照常运行。

## 安装和登录 Harness

`./install`（[安装](install.md)）的前提只有一条：机器上已装好并登录了 Claude Code 或 Codex 之一。安装器和机器人都不替你安装它们、不替你登录；安装时对每个检测到的 Harness 真实调用一次模型，没登录、网络不通会逐个说明，没调通的不启用。

- **Claude Code**：按[官方说明](https://docs.claude.com/en/docs/claude-code/setup)安装并登录（`claude` 里完成登录；验证：`claude -p "ok"` 有回答）。
- **Codex**：按[官方说明](https://github.com/openai/codex)安装并登录（`codex login`；验证：`codex exec "ok"` 有回答）。
- **要代理才能访问模型的机器**：在运行 `./install` 的终端里先 `export HTTPS_PROXY=http://主机:端口`（探测像网络不通时安装器也会问一次）；探测通过后代理地址和飞书域名的 `NO_PROXY` 一起写进服务环境。不能带用户名密码。

### 进阶 Harness（dsh、Pi、扩展的）

下面这些要自己的配置，一遍式安装不探测、不自动启用；装好后在 `harnesses.json` 的 `enabled_harnesses` 里加上，见[配置 Harness 参数](#配置-harness-参数)。



| Harness | 准备什么 | 安装器怎么判断 |
| --- | --- | --- |
| Codex | 安装 `codex` CLI 并登录。启动卡的 Codex 模型取自 CLI 生成的 `~/.codex/models_cache.json`；写好 Acme 个人密钥后还会列出 Acme 的 `gpt-6-astra`、`gpt-6.1-sol` 等模型 | PATH 里有 `codex` |
| Claude Code | 安装 `claude` CLI 并登录 | PATH 里有 `claude` |
| dsh（DeepSeek Harness） | `npm install -g @deepseek-ai/dsh`，并写好 Acme 个人密钥；它运行 Acme 的 `ali-deepseek-v4.1-flash` 等模型，见 [Acme 模型](harnesses.md#模型网关codexpi-与-dsh) | PATH 里有 `dsh`；另报告是否找到 Acme 密钥 |
| Pi | `npm install --prefix <目录> @earendil-works/pi-coding-agent@1.5.2`（不是 `@mariozechner/pi`），在 `--harness-config` 里写 `"pi": {"args": ["--pi-cmd", "<目录>/node_modules/.bin/pi"]}`；服务的 PATH 里要有 Node.js 22.19+。它跑 Codex 登录（`~/.codex/auth.json`，只借用访问令牌）和 Acme 的 GPT 模型，见 [Pi](harnesses.md#pi) | PATH 里有 `pi`（装在别处时只看 `--pi-cmd`） |

Acme 模型用本人的个人密钥（在 [Acme 个人 SK](https://gateway.example.com/keys) 页面创建），写进 `~/.config/harness-bridge/acme-api-key`，权限 `600`，命令见 [Acme 模型](harnesses.md#模型网关codexpi-与-dsh)。不再需要 `codex-w`、`claude-w` 包装器。

准备好后，在每个要用的 CLI 里用自己的账号发一次真实请求。模型出现在列表里不代表当前账号能调用。可执行文件不在 PATH 里时，在 Harness 配置里指定路径，见[配置 Harness 参数](harnesses.md#配置-harness-参数)。

CLI 登录和后面注册服务要在同一个系统账号、同一套环境变量下进行。注册服务的终端里设置的 `PATH`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME`、代理变量和所有 `LARK_*BRIDGE_*` 设置会写进服务清单，开机恢复后仍按它们运行；名字像凭据的变量不会写入。规则见[服务清单里的环境变量](configuration.md#服务清单里的环境变量)。

## 配置 Harness 参数

### `--harness-config` 文件

除 Codex 外，各 Harness 的启动参数写在一个 JSON 文件（`harnesses.json`）里，`./install` 会写好它（只含调通的 Harness）；要加自己的参数时，经 `--harness-config` 交给 bootstrap（见[运维指南](operations.md#注册与启动服务)）：

```json
{
  "enabled_harnesses": ["claude", "codex", "dsh", "pi"],
  "harnesses": {
    "claude": {"args": ["--claude-cmd", "/absolute/bin/claude"]},
    "dsh": {"args": ["--dsh-cmd", "/absolute/bin/dsh"], "models_first": ["ali-deepseek-v4.1-flash"]},
    "pi": {"args": ["--pi-cmd", "/absolute/pi-runtime/node_modules/.bin/pi"]}
  }
}
```

```bash
./scripts/bootstrap_harness_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" \
  --harness-config /absolute/harnesses.json
```

| 键 | 含义 |
| --- | --- |
| `harnesses.<ID>.args` | 按命令行逐项书写的字符串数组。不是字符串数组或参数名写错时机器人启动失败。`codex` 键不读 `args` |
| `harnesses.<ID>.models_first` | 可选：这个 Harness 的模型先列哪几个（模型 ID；一个模型由多个预设合成一项的 Harness 写其中任一预设的 ID），目录里没有的跳过，最近选过的仍在最前。`codex` 键只读这一项 |
| `enabled_harnesses` | 可选：只启用这些 Harness，第一项是新话题的默认 Harness；省略时启用全部、默认 Codex。必须非空、不重复、只用支持的 ID |

- 不在名单里（含已下架）的 Harness 键只记一行 `harness config ignores Harnesses not in the roster`，然后忽略。
- `--state-dir`、`--workdir`、`--profile` 由机器人统一传入，写了也不生效。文件里不写凭据。
- 服务和每个任务进程启动时按路径重新读，文件放持久位置、一次写完整；重启后生效。
- 用了 `enabled_harnesses` 时已有话题保留原 Harness，原后端被禁用后不改由其他后端执行。

### 常用参数

Codex 的参数直接写在 bootstrap 命令上（`setup_harness_bridge.sh` 不接受，见[注册与启动服务](operations.md#注册与启动服务)），其余写进 `harnesses.json` 的 `args`。

| Harness | 参数（默认值） |
| --- | --- |
| Codex（bootstrap） | `--sandbox read-only\|workspace-write\|danger-full-access`（`danger-full-access`）；`--codex-cmd`（`codex`）；`--codex-timeout`（`0`）；`--model`（`gpt-6-astra`）；`--reasoning-effort`（`max`） |
| Claude | `--claude-cmd`（`claude`）；`--claude-timeout`（`0`）；`--model`（`claude-fable-5`）；`--effort`（`xhigh`）；`--no-skip-permissions`；`--claude-login-shell`；`--claude-auto-compact-window`、`--claude-compact-between-turns`（都不设置） |
| dsh | `--dsh-cmd`（`dsh`）；`--dsh-timeout`（`0`）；`--model`（`ali-deepseek-v4.1-flash`）；`--reasoning-effort`（`high`） |
| Pi | `--pi-cmd`（`pi`）；`--pi-timeout`（`0`）；`--model`（`gpt-6-astra`）；`--reasoning-effort`（`high`） |

- 超时单位是秒，`0` 或负数表示不限时长。
- 没有上次选择时启动卡预选 Codex 的 `--model`（不在目录里时选第一项）；预算参数不影响预选。
- 兼容的旧选项（现网 `harnesses.json` 仍能解析）：`--claude-transport stream-json`、`--codex-transport app-server`（只接受这一个值）；`--traex-ppe-env`、`--traex-site`（隐藏，值被忽略）。
- 扩展的 Harness 可以另有读环境变量的参数（见扩展的 README），命令行和 `--harness-config` 优先。以前给其余参数设默认值的 16 个变量不再读取（`core/ignored_env.py` 的 `IGNORED_ENV`，设了启动时各记一行），见[不再读取的变量](configuration.md#服务清单里的环境变量)。

### 超时与结束等待

根任务给出最终回复后，还要等子任务、待处理消息和进程清理收尾。设了任务超时时，等待不超过任务的截止时间（`core/completion.py` 的 `CompletionBudget`）。这些限制不改变 Harness 自身的模型请求重试。

- **Claude**：后台命令或子 Agent 结束时 Claude Code 会再起一轮通知，模型可以再回复一次；机器人等到后台任务全部结束，只受 `--claude-timeout` 约束。因停止或超时结束、已有完整回复的照常发出并加说明（`cancelled`、`background_timeout`，会话不续接）。

## Codex

**准备**：安装 `codex` CLI 并用本人账号登录。任务开始前只检查可执行文件。

**运行方式**：每个任务一个 `codex app-server`，经 stdio 通信。

- 命令、文件修改、MCP 工具、计划和子 Agent 都进执行过程。模型、推理强度、工作区和权限每轮传入。`agents.max_threads=4`。
- 桥接规则在新线程首轮的用户输入开头，续接轮附提醒和约定（[一次运行](architecture.md#一次运行)）；`developerInstructions` 只有 Codex 自己的说明。
- 原生提问：不需要停下的显示在任务卡上，其他工作继续；必须先回答的让本轮先结束，回答后在同一会话继续。
- 工作区只读工具：每个会话挂本任务专用的 `bridge_workspace` MCP 服务，可读工作区、附件目录和换配置的 `handoff/`，拒绝 `..`、符号链接和二进制；实际权限只由 `--sandbox` 决定。
- 下载请求与结果：从 rollout 还原本轮每次请求的 `input`，子 Agent 和 fork 出的线程也算；不记的部分列在 `not_recorded`。

**本轮结果**：thread 和 turn 双重绑定，只用当前 turn 的完整 items，不完整时按 ID 回读；取 `final_answer` 阶段的 agentMessage。

**执行指标**：token 取 `thread/tokenUsage/updated` 的累计减本轮基线（`CodexTurnUsage`），子 Agent 线程各自减基线；生成时间从 `turn/start` 计，扣除工具和提问区间（重试也在里面，token/s 会偏低）；TTFT `codex_item`、`codex_delta`。

**网关模型**见[模型网关](#模型网关codexpi-与-dsh)。

## Claude Code

**准备**：安装 `claude` CLI 并登录。任务开始前运行 `claude auth status --json`，未登录时不执行；通过后 5 分钟内沿用结果（`app/availability.py` 的 `LOGIN_REUSE`）。环境要靠交互式 shell 才齐全时写 `--claude-login-shell`，任务、模型目录和回退文件的命令经 `bash -ic` 启动；登录检查不经这层 shell，`--claude-cmd` 仍要在 `PATH` 上或写绝对路径。

**运行方式**：每轮一个双向 `claude -p --input-format stream-json` 进程。

- Bridge 不改系统提示：桥接规则在新会话首轮的用户消息开头，续接轮附回复格式提醒、工作区和检查点约定；续接的会话沿用首次调用的系统提示。
- 只回答本轮根任务发出、和已观察到的工具调用一致的 `AskUserQuestion`（1 至 4 题、每题 2 至 4 个选项）；保密问题、子 Agent 的提问和其他权限请求一律拒绝。待答问题属于原任务进程，进程结束后失效。
- 补充要等 Claude 进入会话后才能接收，之前先保存；送达未确认的输入不自动重发。图片格式不支持时本轮失败。

**压缩**：一轮中途（可选）`--claude-auto-compact-window TOKENS` 给子进程设 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`。轮间压缩在续接会话的一轮开始前做：Bridge 要求（`compact_first`，如主线程的阈值）或设了 `--claude-compact-between-turns TOKENS` 且上一轮结束时的上下文达到它；用单独的 `claude -p --resume <会话>` 发 `/compact`，会话 ID 不变，token 算进本轮，执行过程里单独一行；失败或 20 分钟没完成时按原上下文继续。

**回退一步**（[用法](usage.md#回退一步)、[机制](architecture.md#回退一步)）：子进程带 `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`。去掉一轮用 `--resume <会话> --resume-session-at <保留条目>`，改回文件用 `claude -p --resume <会话> --rewind-files <提问 uuid>`。

**交接摘要**（`backends/claude/handoff.py`，[用法](usage.md#换配置)）：话题从有会话的 Claude 绑定换走后，新绑定的第一轮先用原模型和原思考预算在原工作目录跑 `claude -p --resume <旧会话> --fork-session`，请它写一份不超过约 1500 字的交接摘要，最多等 180 秒。其他参数和平常一轮相同（缓存才对得上），只是不带 `--dangerously-skip-permissions` 和主线程的 Bridge 工具：问到 Bridge 的权限请求一律拒绝，只读工具和 `permissions.allow` 放行的工具挡不住，靠提示词里的“不要调用任何工具”。旧会话文件不动，分叉的会话文件用完删掉。旧绑定或话题现在开着脱敏时经中继，中继起不来、设置读不出就不发；这个字段之前换的配置、换时还有一步回退没被接走的、这一轮会因工作目录不在或脱敏被拒的、旧会话被安全拒绝或官方模型降级停过、是拒绝后的重试或已禁止续接的都不做。

**子 Agent 的模型**（`backends/claude/subagents.py`，设置卡“新话题”页的“子 Agent”一行，见 [main-thread.md 设置卡](main-thread.md#设置卡)）：默认“轻量”，每一轮的 Claude 子进程（话题、主线程、轮前压缩，不含目录查询）带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，再用 `--agents` 把内置的 Explore 换成只读（Read、Grep、Glob、Bash）、跑在 Haiku 上的同名子 Agent；这一轮的模型是 Haiku 时不设前者。“跟主模型”两样都不加，所有子 Agent（内置 Explore 也是）跑主模型（2.1.318 实测）。Claude Code 接的网关认不出 `sonnet`、`haiku` 这两个别名的部署，把“子 Agent”设成“跟主模型”。

**下载请求与结果**：从会话记录的 `api-request*` 行还原本轮每次请求，按 `requestDigest` 核对上的记 `digest_verified`。

**模型目录**：一次双向初始化，不提交 prompt、不加载项目工具。档位只取模型声明的 `low` 到 `max`，声明了 `xhigh` 的另有 `ultracode`。子进程带 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`，经 `ANTHROPIC_BASE_URL` 接托管网关时还列出网关 `/v1/models` 的模型（如 `claude/claude-opus-5`）；某次一个都没列出时沿用 `<state>/native-models/claude-gateway.json`（24 小时内）。已删除的网关模型的话题在启动前失败，提示换配置。

**本轮结果**：只取当前调用的 JSONL：Session 一致、result 正常，result 文本等于最后一条完整根消息，核对输入回显和最终 `idle`。有结果后 CLI 非零退出时按部分结果投递（会话封住）；最终 result 之后才结束的子任务让本轮失败（正文可重投，会话不续接）。原生错误详情在 `<状态目录>/claude-diagnostics/<run-id>.json`，不要外传。

**等待期限与收尾**：最终 result 之后关输入，约 10 秒后停止进程组，按最后的根 result 定结果（`settle_failure`）；没有 result 记 `settle_timeout`。还有补充没被接收时不关输入。进程组停不掉时正文只保存不发送。

**限流与额度**：429 记 `rate_limited`（会[自动重试](usage.md#自动重试)），529 记 `root_failed`，402 记 `quota_exhausted`，451 记 `model_blocked`（后两种提示换配置）。后台子 Agent（`async_launched`）到它的 `task_notification` 才结束。

**执行指标**：输出 token 是每条消息最大的 `output_tokens` 之和；缓存命中、写入取 `cache_read_input_tokens`、`cache_creation_input_tokens`；子任务（`ClaudeSubtaskUsage`）按 `parent_tool_use_id` 算，退出后读 `subagents/agent-*.jsonl` 补值；上下文 `context_tokens`、`context_window` 取 result 的 `modelUsage`；生成时间扣除 tool_use 到 tool_result；TTFT `claude_delta`。

**官方拒绝与降级停止**：明确的拒绝或 fallback 会停止当前任务及其本机后代进程，不自动重试，见[官方拒绝与降级停止](policy-stop.md)。

## Pi

Pi 是 pi（npm 包 `@earendil-works/pi-coding-agent`，原 `@mariozechner/pi-coding-agent`），没有沙箱的终端编码 Agent，用来跑 GPT 模型：Codex 的 ChatGPT 登录（`openai-codex`，和 Codex 共用额度）和 Acme。

- **安装**：固定 1.5.2（2026-11-12 起；之前 0.73.1，适配两版都能用），装进单独的目录，`--pi-cmd` 指向 `<目录>/node_modules/.bin/pi`；需要 Node.js 22.19+。`@mariozechner/pi` 是另一个工具。
- **运行方式**：每轮一个 `pi --mode rpc --session-dir <状态目录>/pi-sessions [--session|--fork <会话文件>] --provider openai-codex|acme --model <模型> --thinking <档位>`，JSONL 收发：`get_state`、`prompt`、补充 `steer`、停止 `abort`。续接和分叉只传会话文件路径，找不到时不启动。
- **私有目录**：每轮新建 `<状态目录>/tmp/pi-home-*` 作为 `PI_CODING_AGENT_DIR`，结束删除，里面不写凭据：`models.json` 里提供商的 `apiKey` 是 `!printenv <变量名>`（1.x 把裸变量名当成密钥本身，`openai-codex` 也不再读 `auth.json` 的 api_key 条目），`auth.json` 是空对象。
- **凭据**：Codex 的访问令牌每轮从 `$CODEX_HOME/auth.json` 现读，经 `PI_CODEX_ACCESS_TOKEN` 交给 pi，refresh token 从不交出；离过期不到 1 小时或被拒时不启动，提示先在 Codex 跑一轮。Acme 轮只给 `ACME_API_KEY`。`shellCommandPrefix` 去掉这两个变量。
- **思考预算**：`max` 用 `--thinking xhigh`，本轮 `models.json` 映射成线上的 `max`；没有 `ultra`。Acme 走中继（[繁忙重试](#模型网关codexpi-与-dsh)），仍限流时写 `BUSY_TEXT`。
- **本轮结束**：`agent_end` 后再 `get_state` 确认 `isStreaming:false`、`isCompacting:false`；正文取最后一条 `stopReason:"stop"` 的助手消息。
- **执行指标**：输入是 `input + cacheRead + cacheWrite`；快过 1000 token/s 的请求不计时；TTFT `pi_delta`。
- **补充**：`steer` 成功即“后端已接收”，pi 开始这条用户消息即“模型已读取”；以 `/` 开头的前加“用户补充：”。
- 读工作目录及上级的 `AGENTS.md`、`CLAUDE.md` 和 `.agents/skills`，不读 `~/.pi/agent`。
- **限制**：没有原生问答、子 Agent、脱敏、回退一步、下载请求与结果。

## 模型网关（Codex、Pi 与 dsh）

模型网关是“OpenAI Responses / Anthropic Messages 兼容”的上游，由配置描述，可以有多个；核心不带任何一家的地址。配置来源两处：扩展 manifest 的 `GATEWAYS`（`core/extensions.py`），和 `harnesses.json` 顶层的 `gateways`（自己部署时配自己的网关）。每个网关一个名字，字段：

```json
{"gateways": {"mygw": {
  "display": "MyGW", "prefix": "mygw/", "base_url": "https://gateway.example",
  "key_env": "MYGW_API_KEY", "key_file": "harness-bridge/mygw-api-key",
  "headers": {"x-source": "bridge"}, "harnesses": ["codex", "pi", "dsh"],
  "quota_pattern": "quota exceeded", "busy_hint": "一直如此时到用量页面看一下额度"}}}
```

- 必填 `base_url`、`key_env`；给 Codex 或 Pi 用的要有 `prefix`（模型 id 前缀，如 `mygw/gpt-6`）；给 dsh 用的网关，模型 id 不带前缀，dsh 只用第一个声明它的网关。可选 `base_url_env`、`key_file_env`、`header_envs`（头名到环境变量）覆盖地址、密钥文件和头的值。
- 密钥先读 `key_env` 指的环境变量，再读 `key_file`（相对路径在 `$XDG_CONFIG_HOME` 或 `~/.config` 下，默认 `harness-bridge/<名字>-api-key`，必须本人所有、权限 `600`）。
- `quota_pattern`：403 响应里表示额度用完的正则；`busy_hint`：繁忙失败卡的末句。
- 没有配网关时 Codex 和 Pi 只剩各自账号的模型，dsh 没有模型可选；一个网关的密钥没配时只是它的模型不出现。状态目录里的目录缓存是 `native-models/<名字>.json`，繁忙日志是 `<名字>/relay.log`。

公司部署里配的网关（名字、地址、密钥怎么拿）写在扩展自己的 README 里（「Acme 模型」一节）。

### dsh（DeepSeek Harness）

dsh 是 DeepSeek 官方开源的 Agent Harness（npm 包 `@deepseek-ai/dsh`，预览版），运行 Acme 的 Anthropic Messages 模型。

- **安装**：`npm install -g @deepseek-ai/dsh`，按 `0.2.1-rc.2` 验证，升级后跑[真实环境验证](testing.md#真实环境验证)。只检查可执行文件和 Acme 密钥。
- **运行方式**：每轮一个 `dsh --profile headless --patch <补丁> --json [--session-id <会话>] -`，正文从 stdin 传。补丁是本轮私有的临时文件：声明 `acme` provider、设所选模型和档位（经 `output_config.effort`）、关闭起标题的请求。环境 `DSH_PERMISSION_MODE=danger-full-access`、`DSH_TELEMETRY_MODE=DISABLED`。
- **本轮结果**：首行 `session` 一致、有 `turn_start`、最后 `turn_end` 为 `completed`、零退出码，正文取无截断的 `final`。事件每步完成才发出，没有 TTFT。
- **续接**：`--session-id`，只能在创建会话时的工作目录。
- **限制**：没有原生问答、执行中补充、原生图片、原生 fork；网页搜索要 `DEEPSEEK_API_KEY`。

## 执行权限

默认面向个人开发机：Harness 以运行机器人的账号执行本机命令，能读写它能访问的所有文件。用自己的开发账号，不要把状态目录复制给同事。

| Harness | 默认 | 收紧方式 |
| --- | --- | --- |
| Codex | sandbox `danger-full-access`，审批策略 `never` | bootstrap 加 `--sandbox workspace-write` 或 `--sandbox read-only` |
| dsh | `DSH_PERMISSION_MODE=danger-full-access`，审批 `never` | 没有开关：headless 没人能批准请求 |
| Pi | 没有沙箱和权限确认 | 没有开关 |
| Claude | 传 `--dangerously-skip-permissions` | `harnesses.json` 写 `--no-skip-permissions` |

- 以前收紧权限的 `LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 不再读取；设了非默认值时诊断和部署预检失败，见[权限变量让诊断或部署失败](troubleshooting.md#权限变量让诊断或部署失败)。
- 权限每轮按服务的当前配置传入，重启后已有话题的下一轮也按新设置。收紧后不会请你确认，无人值守的任务可能因权限不足失败，先用小任务验证（[真实环境验证](testing.md#真实环境验证)）。
- Codex `workspace-write`、`read-only` 下命令可能无法联网；`workspace-write` 下 `checkpoints/` 也可写。

## 适配器结构

给改代码的人。后端契约（`core/harness.py` 的 `HarnessSpec`、`BackendRuntime`、`HarnessCapabilities`）和怎么加后端见[架构与开发说明](architecture.md)；后端不导入 `app/`、`feishu/`、`cards/` 和别的后端（`backends/acme` 除外），见[硬约束](architecture.md#硬约束)。每个后端的 `bridge.py` 是 Harness 规格、参数和后端类；`sessions.py` 是原生会话位置；其余模块开头的 docstring 写着它管什么。

| 后端 | 代码位置 |
| --- | --- |
| Claude Code（`backends/claude/`） | `runtime.py` 一轮的收发与收尾（`run_stream`、空闲压缩 `compact_only`）；`process.py` CLI 进程和 JSONL 传输（`ClaudeProcess`、`TRANSPORT_FAILURES`）；`control.py` 输入、补充、问答和权限请求（`ClaudeControl`）；`argv.py` 启动参数和环境（`stream_argv`、`turn_env`）；`events.py` 事件（`ClaudeEventAdapter`）；`completion.py` 结果边界；`usage.py` 用量和 TTFT；`models.py` 目录行；`compaction.py`、`rewind.py`、`export.py` 压缩、回退、下载；`policy.py` 官方拒绝；`redaction.py` 脱敏中继 |
| Codex（`backends/codex/`） | `appserver.py` 一轮的开线程、起 turn、收尾（`run_appserver`）；`rpc.py` App Server 进程与 JSON-RPC（`AppServer`、`RpcError`）；`turn.py` 读一个 turn 到结束（`read_turn`、`close_turn`）；`events.py` 事件（`CodexEventAdapter`）；`usage.py` 用量；`gateway.py` 线程 provider；`workspace_reader.py` 只读 MCP；`export.py` 下载 |
| Pi（`backends/pi/`） | `home.py` 每轮私有目录、凭据和预算；`rpc.py` 一轮 RPC 的收发、补充、停止和结束判断（`run_rpc`，读循环 `_read_loop`、关闭 `_close`）；`events.py` 事件 |
| dsh（`backends/dsh/`） | 只有 `bridge.py`：进程、补丁、事件（`DshEventAdapter`）、结果 |
| 模型网关（`backends/gateway/`，配置在 `core/gateways.py`） | `connection.py` 密钥、目录、请求头、`BUSY_WINDOW`；`relay.py` 中继；`catalog.py` 目录行和繁忙提示（Codex、Pi 共用） |


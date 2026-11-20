# Harness 说明

Harness 是执行任务的 Agent 程序。本篇按上手顺序写：有哪些、各能做什么 → 怎么装和登录 → 启动卡上的模型从哪来 → 每个 Harness 的行为和参数 → 模型网关 → 在设备上运行、执行权限 → 改 `harnesses.json` 和参数 → 适配器代码。飞书里怎么用见[使用指南](usage.md)，启动卡缺模型见[排障](troubleshooting.md#启动卡里缺少某个-harness-的模型)。

## 支持的 Harness

核心带五个：Codex、Claude Code、DeepSeek Harness（dsh）、Pi、Antigravity（`core/roster.py`），[扩展](architecture.md#扩展)可以再加（见扩展自己的 README）。**供应商**是模型从哪里来（账号、网关或 API），各有自己的模型和思考预算（`core/providers.py` 的 `PROVIDERS`，`provider_of` 按模型 ID 前缀认）。一个 Harness 可以有几个供应商。

| Harness | 供应商（模型 ID 前缀） | 登录或密钥 |
| --- | --- | --- |
| Claude Code | 本机 Claude Code 的登录（无前缀，换配置卡写“Claude 自带”） | `claude` 自己的登录 |
| Codex | 自己的登录（无前缀）；配了模型网关时还有网关（`<网关前缀>`，如 `mygw/`） | Codex 的登录；网关的个人密钥 |
| DeepSeek Harness | 只有模型网关（无前缀）：没有给 dsh 用的网关就用不了 | 网关的个人密钥 |
| Pi | Codex（无前缀，写“Codex 自带”）；配了模型网关时还有网关 | Codex 的 ChatGPT 登录（只借用访问令牌）；网关的个人密钥 |
| Antigravity | 自己的登录（无前缀，写“Antigravity 自带”） | agy 的 Google 账号登录 |

核心不带任何一家网关，网关由 `harnesses.json` 的 `gateways` 或扩展配置（[模型网关](#模型网关codexpi-与-dsh)）。已下线的 Harness 和供应商（`RETIRED_PREFIXES`）、旧话题会怎样，见[已下线的功能与旧配置](legacy.md)。

| ID（启动卡名称） | 运行方式 | 原生问答 | 执行中补充 | 原生图片 | 分叉的会话 | 原生会话位置 |
| --- | --- | --- | --- | --- | --- | --- |
| `codex`（Codex） | 每个任务一个 `codex app-server` | 支持，含多选 | 支持 | 支持 | `thread/fork` | `$CODEX_HOME/sessions`，默认 `~/.codex/sessions` |
| `claude`（Claude） | 每轮一个双向 `claude -p`（stream-json） | 根任务单选、多选、自由文本 | 支持，可到“模型已读取” | 支持 | `--resume --fork-session` | `$CLAUDE_CONFIG_DIR/projects/<目录编码>`，默认 `~/.claude/projects/…` |
| `dsh`（DeepSeek Harness） | 每轮一次 `dsh --profile headless --json` | 无，经检查点提问 | 无 | 无 | 不支持 | `$DSH_HOME/sessions`，默认 `~/.dsh/sessions` |
| `pi`（Pi） | 每轮一个 `pi --mode rpc` | 无，经检查点提问 | 支持，可到“模型已读取” | 支持 | `--fork <会话文件>` | `<状态目录>/pi-sessions/<时间>_<会话 ID>.jsonl` |
| `agy`（Antigravity） | 每轮一个 `agy` headless（stream-json），补充时换一个续接的进程 | 无，经检查点提问 | 支持：停下当前一步再续接 | 无（给路径，它自己读图） | 不支持 | `~/.gemini/antigravity-cli/conversations` |

- **原生问答**：Claude 的问题在原任务里等回答；Codex 必须回答的问题先结束本轮，回答排成续接任务。所有 Harness 都能经检查点提问（[回答模型的问题](usage.md#回答模型的问题)）。
- **原生图片**：不支持时只给本地路径。Claude、Pi 单张超过 5 MiB，或 Claude 合计超出 8 MiB 帧时，放不下的只给路径（`app/runs.py` 的 `_fit_images`）。
- **分叉**：管家分叉出的话题（[开话题、转交、分叉](butler.md#开话题转交分叉)）复制原话题的原生会话；不支持或原话题正在执行时改用已完成的对话记录。**换配置**除只改脱敏外都开新会话。
- **只有 Claude Code 有的**：[敏感词脱敏](usage.md#敏感词脱敏)、[回退一步](usage.md#回退一步)、限流（429）后[自动重试](reference.md#自动重试)、轮前压缩。“下载请求与结果”只有 Claude Code 和 Codex 有。

## 安装和登录 Harness

`./install`（[安装](install.md)）的前提只有一条：机器上已装好并登录了 Claude Code 或 Codex 之一。安装器和机器人都不替你安装、不替你登录；安装时只对 Claude Code 和 Codex 各真实调用一次模型（`service/installer.py` 的 `PROBED`），没登录、网络不通会逐个说明，没调通的不启用。

- **Claude Code**：按[官方说明](https://docs.claude.com/en/docs/claude-code/setup)安装，在 `claude` 里登录；验证：`claude -p "ok"` 有回答。
- **Codex**：按[官方说明](https://github.com/openai/codex)安装，`codex login`；验证：`codex exec --skip-git-repo-check "ok"` 有回答（不在 git 仓库里不加这个参数会被拦下）。
- **要代理才能访问模型**：运行 `./install` 前先 `export HTTPS_PROXY=http://主机:端口`（探测像网络不通时安装器也会问）；探测通过后代理和飞书域名的 `NO_PROXY` 一起写进服务环境。不能带用户名密码。

CLI 登录和注册服务要用同一个系统账号、同一套环境变量。注册时终端里的 `PATH`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME`、代理变量和所有 `COVEY_*`（含旧名）写进服务清单，开机恢复后仍按它们运行；名字像凭据的不写入，见[服务清单里的环境变量](configuration.md#服务清单里的环境变量)。

### 进阶 Harness（dsh、Pi、Antigravity、扩展的）

这些要自己准备，`./install` 不探测、不自动启用；装好后按[手工加一个 Harness](#手工加一个-harness) 加进 `harnesses.json`。扩展的 Harness 由扩展的安装钩子在 `./install` 第 3 步报告并启用，准备方法看扩展的 README。

| Harness | 准备什么 |
| --- | --- |
| dsh | `npm install -g @deepseek-ai/dsh`，再配一个给 dsh 用的 Anthropic Messages 网关（[最小网关示例](#最小网关示例)），没有网关用不了 |
| Pi | `npm install --prefix <目录> @earendil-works/pi-coding-agent@1.5.2`，`--pi-cmd` 指向 `<目录>/node_modules/.bin/pi`；服务的 PATH 里要有 Node.js 22.19+。见 [Pi](#pi) |
| Antigravity | 官方脚本 `https://antigravity.google/cli/install.sh` 安装（装到 `~/.local/bin/agy`），在运行机器人的账号下跑一次 `agy` 选 Google OAuth 登录。见 [Antigravity](#antigravity) |

准备好后在每个 CLI 里用自己的账号发一次真实请求：模型出现在列表里不代表当前账号能调用。可执行文件不在 PATH 里时，用 `--<名字>-cmd` 写绝对路径（[常用参数](#常用参数)）。

## 选模型

启动卡的模型每项写作“Harness · 供应商 · 模型名”，Harness 自己的供应商不写（“Claude · Opus 5.5”“Codex · MyGW · GPT-6 Astra”），Pi 两个供应商都写出。选模型就选定了 Harness 和供应商，之后可以[换配置](usage.md#换配置)。没有上次选择时预选 Codex 的 `--model`（不在目录里时选第一项）；`models_first` 可以把常用的排到前面（[`--harness-config` 文件](#--harness-config-文件)）。

### 模型目录

启动卡只列出各 Harness 实时读到的模型（`app/launch_models.py`）：后台每 5 分钟刷新，15 分钟内没刷新成功的 Harness 从启动卡上消失（只是超时没回应时上次的目录最多再用 1 小时，`KEEP_UNANSWERED`），点卡片时不做慢查询。目录里标为弃用、移除或隐藏的不列；失败时 `bridge.log` 记 `launch catalog unavailable harness=<ID> error=<错误类名> detail=<原因>`。

| Harness | 目录来源 |
| --- | --- |
| Codex | `~/.codex/models_cache.json` 里 `visibility` 为 `list` 的模型，加各网关 `/v1/models` 里 `openai-responses` 协议的模型（`<网关前缀><模型>`）；都读不到时 Codex 不出现 |
| Claude | 一次 `claude` 双向初始化返回的模型和档位；接托管网关时含网关 `/v1/models` 的模型 |
| dsh | 给 dsh 用的网关 `/v1/models` 里 `anthropic-messages`（或 `anthropic`）协议的模型 |
| Pi | 和 Codex 相同的两处来源，只取 `gpt` 开头的，去掉 `ultra` 档 |
| Antigravity | `agy models` 列出的（当前 Google 账号能用的）。末尾是档位的 ID（`gemini-4-neon-high`）合成一个模型、档位做思考预算，没有档位的单列；没登录时不出现 |

另外不列：扩展按自己规则隐藏的模型和档位；Claude 的旧版本（`claude-sonnet-`、`claude-haiku-`、`claude-opus-4-` 开头的，带不带网关前缀都算；别名 `sonnet`、`haiku` 照常列出）；网关目录里的 `auto`、`auto-max`。已固定在这些模型上的话题照常运行。

## Codex

- **准备**：装 `codex` 并用本人账号登录；任务开始前只检查可执行文件。
- **运行方式**：每个任务一个 `codex app-server`，经 stdio 通信。命令、文件修改、MCP 工具、计划和子 Agent 都进执行过程；模型、推理强度、工作区和权限每轮传入；`agents.max_threads=4`。桥接规则在新会话首轮的用户输入开头，续接轮附提醒和约定（[一次运行](architecture.md#一次运行)）。
- **原生提问**：不需要停下的显示在任务卡上，其他工作继续；必须先回答的让本轮先结束，回答后在同一会话继续。
- **工作区只读工具**：每个会话挂本任务专用的 `bridge_workspace` MCP 服务，可读工作区、附件目录和换配置的 `handoff/`，拒绝 `..`、符号链接和二进制；实际权限只由 `--sandbox` 决定。
- **本轮结果**：thread 和 turn 双重绑定，只用当前 turn 的完整 items（不完整时按 ID 回读），取 `final_answer` 阶段的 agentMessage。下载请求与结果从 rollout 还原，不记的部分列在 `not_recorded`。
- **执行指标**：token 取 `thread/tokenUsage/updated` 的累计减本轮基线（`CodexTurnUsage`），子 Agent 会话各自减基线；生成时间从 `turn/start` 计，扣除工具和提问区间（重试也算在内，token/s 会偏低）；TTFT `codex_item`、`codex_delta`。
- **参数**写在 bootstrap 命令上，不进 `harnesses.json`（[常用参数](#常用参数)）；网关模型见[模型网关](#模型网关codexpi-与-dsh)。

## Claude Code

- **准备**：装 `claude` 并登录。任务开始前跑 `claude auth status --json`，未登录不执行；通过后 5 分钟内沿用结果（`app/availability.py` 的 `LOGIN_REUSE`）。环境要靠交互式 shell 才齐全时加 `--claude-login-shell`（任务、模型目录和回退的命令经 `bash -ic` 启动；登录检查不经这层，`--claude-cmd` 仍要在 `PATH` 上或写绝对路径）。
- **运行方式**：每轮一个双向 `claude -p --input-format stream-json`。Bridge 不改系统提示：桥接规则在新会话首轮的用户消息开头，续接轮附格式提醒、工作区和检查点约定。
- **问答**：只回答本轮根任务发出、和已观察到的工具调用一致的 `AskUserQuestion`（1 至 4 题、每题 2 至 4 个选项）；保密问题、子 Agent 的提问和其他权限请求一律拒绝。待答问题属于原任务进程，进程结束后失效。
- **补充**：Claude 进入会话后才能接收，之前先保存；送达未确认的不自动重发。图片格式不支持时本轮失败。
- **压缩**：`--claude-auto-compact-window TOKENS` 给子进程设 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`（一轮中途）。轮间压缩在续接的一轮开始前做：Bridge 要求（`compact_first`，如管家的阈值），或设了 `--claude-compact-between-turns TOKENS` 且上一轮结束时上下文达到它；用单独的 `claude -p --resume <会话>` 发 `/compact`，会话 ID 不变，token 算进本轮；失败或 20 分钟没完成按原上下文继续。
- **回退一步**（[用法](usage.md#回退一步)、[机制](architecture.md#回退一步)）：子进程带 `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`；去掉一轮用 `--resume-session-at`，改回文件用 `--rewind-files`。
- **交接摘要**（`backends/claude/handoff.py`）：话题从有会话的 Claude 绑定换走后，新绑定的第一轮先用原模型在原目录 `--resume <旧会话> --fork-session`，请它写一份约 1500 字以内的交接摘要（最多等 180 秒，不带跳过权限和 Bridge 工具，分叉的会话用完删掉）。哪些情况不做见[使用参考 · 换配置之后](reference.md#换配置之后)和[架构](architecture.md#话题中途换配置)。
- **子 Agent 的模型**（`backends/claude/subagents.py`，设置卡“新话题”页的“子 Agent”）：默认“轻量”，子进程带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，并用 `--agents` 把内置 Explore 换成只读、跑在 Haiku 上的同名子 Agent；“跟主模型”两样都不加。Claude Code 接的网关认不出 `sonnet`、`haiku` 别名时选“跟主模型”。
- **模型目录**：一次双向初始化，不提交 prompt。档位只取模型声明的 `low` 到 `max`，声明了 `xhigh` 的另有 `ultracode`。子进程带 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`，经 `ANTHROPIC_BASE_URL` 接托管网关时还列出网关的模型（如 `claude/claude-opus-5`）；某次一个都没列出时沿用 24 小时内的 `<state>/native-models/claude-gateway.json`。已删除的网关模型的话题在启动前失败，提示换配置。
- **本轮结果与收尾**：只取当前调用的 JSONL，result 文本等于最后一条完整根消息才算数。最终 result 后关输入，约 10 秒后停止进程组（还有补充没被接收时不关）；后台命令或子 Agent 结束时 Claude Code 会再起一轮通知，机器人等它们全部结束，只受 `--claude-timeout` 约束，这一轮的回复是各轮回复按顺序连起来（紧挨着的相同段落只留一段，`backends/claude/completion.py` 的 `final_texts`）。有结果后 CLI 非零退出时按部分结果投递（会话封住）；因停止或超时结束、已有完整回复的照常发出并加说明。原生错误详情在 `<状态目录>/claude-diagnostics/<run-id>.json`，不要外传。
- **限流与额度**：429 记 `rate_limited`（会自动重试），529 记 `root_failed`，402 记 `quota_exhausted`，451 记 `model_blocked`（后两种提示换配置）。明确的官方拒绝或降级会停止当前任务，见[官方拒绝与降级停止](policy-stop.md)。
- **执行指标**：输出 token 是每条消息最大的 `output_tokens` 之和；缓存取 `cache_read_input_tokens`、`cache_creation_input_tokens`；子任务按 `parent_tool_use_id` 算，退出后读 `subagents/agent-*.jsonl` 补值；上下文取 result 的 `modelUsage`；生成时间扣除 tool_use 到 tool_result；TTFT `claude_delta`。下载请求与结果从会话记录的 `api-request*` 行还原，按 `requestDigest` 核对上的记 `digest_verified`。

## Pi

Pi 是 pi（npm 包 `@earendil-works/pi-coding-agent`，不是另一个工具 `@mariozechner/pi`），没有沙箱的终端编码 Agent，用来跑 GPT 模型：Codex 的 ChatGPT 登录（`openai-codex`，和 Codex 共用额度）和模型网关。

- **安装**：固定 1.5.2（适配也兼容 0.73.1），装进单独的目录，`harnesses.json` 写 `"pi": {"args": ["--pi-cmd", "<目录>/node_modules/.bin/pi"]}`；需要 Node.js 22.19+。
- **运行方式**：每轮一个 `pi --mode rpc --session-dir <状态目录>/pi-sessions [--session|--fork <会话文件>] --provider openai-codex|<网关名> --model <模型> --thinking <档位>`，JSONL 收发 `get_state`、`prompt`、`steer`（补充）、`abort`（停止）。续接和分叉只传会话文件路径，找不到时不启动。
- **私有目录与凭据**：每轮新建 `<状态目录>/tmp/pi-home-*` 作为 `PI_CODING_AGENT_DIR`，结束删除，里面不写凭据（`models.json` 的 `apiKey` 是 `!printenv <变量名>`，`auth.json` 是空对象）。Codex 的访问令牌每轮从 `$CODEX_HOME/auth.json` 现读，经 `PI_CODEX_ACCESS_TOKEN` 交给 pi，refresh token 从不交出；离过期不到 1 小时或被拒时不启动，提示先在 Codex 跑一轮。网关轮只给网关的 `key_env` 变量；`shellCommandPrefix` 去掉这些变量。
- **思考预算**：`max` 用 `--thinking xhigh`，本轮 `models.json` 映射成线上的 `max`；没有 `ultra`。网关模型走中继（繁忙时重试），仍限流时写 `BUSY_TEXT`。
- **本轮结束**：`agent_end` 后再 `get_state` 确认 `isStreaming:false`、`isCompacting:false`；正文取最后一条 `stopReason:"stop"` 的助手消息。
- **补充**：`steer` 成功即“后端已接收”，pi 开始这条用户消息即“模型已读取”；以 `/` 开头的前加“用户补充：”。
- **执行指标**：输入是 `input + cacheRead + cacheWrite`；快过 1000 token/s 的请求不计时；TTFT `pi_delta`。
- 读工作目录及上级的 `AGENTS.md`、`CLAUDE.md` 和 `.agents/skills`，不读 `~/.pi/agent`。没有原生问答、子 Agent、脱敏、回退一步、下载请求与结果。

## Antigravity

Antigravity 是 Google 的 Agent 开发平台，这里用它的命令行 `agy`（替代了 Gemini CLI），跑当前 Google 账号能用的模型。

- **安装**：`curl -fsSL https://antigravity.google/cli/install.sh | bash`，二进制在 `~/.local/bin/agy`，之后它在后台自己升级；按 1.5.13 验证过。
- **登录**：在运行机器人的系统账号下执行一次 `agy`，选 “Google OAuth”；没有浏览器时它打印授权链接，在自己的浏览器里登录后把授权码贴回终端。没有 D-Bus 时令牌存在 `~/.config/antigravity/`，不进机器人的记录。也可以用 Gemini API 密钥或 Google Cloud 项目登录（[官方说明](https://antigravity.google/docs/install)）。没登录时启动卡上没有它，已有话题的一轮以“还没有登录”失败。
- **运行方式**：每轮一个 `agy --output-format stream-json --input-format stream-json --disable-slash-commands --dangerously-skip-permissions [--conversation <会话>] [--model <模型>] [--effort <档位>]`，正文作为一行 JSON 从 stdin 传入后关闭，跑完这一轮就退出。续接用 `--conversation`，会话 ID 和 `init` 里的不一致时按协议错误失败。
- **本轮结果**：最后一行 `result` 的 `status` 为 `SUCCESS`、零退出码。正文取最后一个工具、子 Agent 或系统步骤之后模型写的字（`result.response` 把这一轮的字全拼在一起，取出的不是它的结尾时退回整个 `response`）；子 Agent 的步骤不算本轮正文。
- **事件与用量**：文字增量是正文和进展说明，工具步骤按名字显示；工具输出和思考不上卡片。用量取每个 `agent_response` 步骤的 `usage`（`input_tokens` 不含缓存读取，`output_tokens` 含思考），没有时才用 `result.usage`。
- **模型和档位**：启动卡的预算就是 `--effort`；模型目录（`launch-models/agy.json`）说没有这个档位时不启动，没有档位的模型不传 `--effort`。
- **失败**：`result.error` 像登录失败记 `authentication_error`、像额度用完记 `quota_exhausted`、像 429 记 `rate_limited`，其余 `root_failed`。agy 的原文不上卡片，stderr 最后几行（去掉登录链接、隐藏凭据）只进 `bridge.log`。
- **补充**是“停下再接着做”（`AgyControl`）：agy 运行中不收新消息，收下补充即“后端已接收”，给 agy 发 SIGINT，它以 `interrupted` 结束并保留已做的部分；它还在跑的命令先 TERM、5 秒后 KILL；然后同一会话起新进程，消息是补充，这时算“模型已读取”。和 Claude 不同，正在执行的那一步会被打断。
- 没有原生问答、原生图片（只给路径，它用自带的 `read_file` 打开）、原生分叉、脱敏、回退一步、下载请求与结果；不能在设备上跑。

## 模型网关（Codex、Pi 与 dsh）

模型网关是“OpenAI Responses / Anthropic Messages 兼容”的上游，由配置描述，可以有多个；核心不带任何一家的地址。配置来自扩展 manifest 的 `GATEWAYS`（`core/extensions.py`）和 `harnesses.json` 顶层的 `gateways`（自己部署时配自己的网关，例子见[最小网关示例](#最小网关示例)）。每个网关一个名字，字段：扩展带的网关（名字、地址、密钥怎么拿）写在扩展自己的 README 里。

- 必填 `base_url`（上游地址）、`key_env`（密钥所在的环境变量）。给 Codex 或 Pi 用的要有 `prefix`（模型 ID 前缀，如 `mygw/gpt-6`）；给 dsh 用的模型 ID 不带前缀，dsh 只用第一个声明它的网关。
- `display` 是卡上的名字；`access` 是换配置卡上“登录或密钥在哪”的提示；`harnesses` 是哪些 Harness 用它（`codex`、`pi`、`dsh` 的子集，默认都用）；`quota_pattern` 是 403 响应里表示额度用完的正则；`busy_hint` 是繁忙失败卡的末句。`headers` 是请求时另带的头；可选 `base_url_env`、`key_file_env`、`header_envs`（头名到环境变量）覆盖地址、密钥文件和头的值。
- 密钥先读 `key_env` 指的环境变量，再读 `key_file`（相对路径在 `$XDG_CONFIG_HOME` 或 `~/.config` 下，默认 `harness-bridge/<名字>-api-key`，必须本人所有、权限 `600`）。
- 没有网关时 Codex 和 Pi 只剩各自账号的模型，dsh 没有模型；一个网关的密钥没配时只是它的模型不出现。目录缓存在状态目录的 `native-models/<名字>.json`，繁忙日志在 `<名字>/relay.log`。Codex、Pi 的网关请求经本地中继，429 时在中继里等一阵再试（`backends/gateway/relay.py`）。

### 最小网关示例

Bridge 从 `GET <base_url>/v1/models` 读模型目录（带 `Authorization: Bearer <密钥>` 和 `headers`），返回 `{"data": [...]}`，每行至少有 `id` 和 `protocol`：`openai-responses` 的给 Codex、Pi，`anthropic-messages`（或 `anthropic`）的给 dsh；没有 `protocol` 的行不列，只按 OpenAI 格式列模型的网关要补上这一项。可选 `display_name`、`supported_reasoning_levels`、`default_reasoning_level`；`status` 为 `deprecated`、`removed` 或带 `hidden` 的不列。Codex、Pi 的请求发到 `<base_url>/v1/responses`，dsh 的发到 Anthropic Messages 接口。

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
- **本轮结果**：首行 `session` 一致、有 `turn_start`、最后 `turn_end` 为 `completed`、零退出码，正文取无截断的 `final`。事件每步完成才发出，没有 TTFT。续接用 `--session-id`，只能在创建会话时的工作目录。
- 没有原生问答、执行中补充、原生图片、原生分叉；网页搜索要 `DEEPSEEK_API_KEY`。

## 在设备上运行

开了多设备（`COVEY_DEVICES=1`，[配置](configuration.md#多设备开发中默认关)）后，工作区写成 `别名:路径` 的话题跑在那台设备上。只有 Claude Code 和 Codex 能在设备上跑（设备的 `harnesses` 只认 `claude`、`codex`）。结构和不变量见[架构 · 设备](architecture.md#设备多设备默认关)。

- **设备上要有的**：Harness 的命令、登录、会话文件、模型目录、代理和证书。Bridge 不替设备登录，也不转发令牌。设备上的环境从空开始，只带设备档案里白名单内的代理和证书变量，再加 `devices.json` 的 `env`；`--claude-cmd`、`--codex-cmd` 在设备上换成自检时记下的路径，登录 shell 写设备的 `login_shell`。
- **启动前**：新加或改了设备先跑自检 `bin/covey_devices.py check <别名>`（[运维](operations.md#设备自检)），它在设备上对每个声明的 Harness 真跑一次最小请求；每轮开始前只读最近的自检，没登录当场拒绝。
- **和本机的差别**：没有回退、下载请求与结果、压缩、分叉和敏感词脱敏（开着脱敏的话题在设备上拒绝）；不取 git diff 快照。补充、原生问答和图片照常，消息里的文件先放到设备的 `.covey/incoming/`；产物和 `show_images` 从设备取回再上传。启动卡列的模型仍是 Bridge 本机的目录。
- **断线**：设备端小程序守着 Agent，Bridge 断线时照常跑；超过耐心值才按中断处理，恢复后先停旧进程再续接。

## 执行权限

默认面向个人开发机：Harness 以运行机器人的账号执行本机命令，能读写它能访问的所有文件。用自己的开发账号，不要把状态目录复制给同事；设备上的进程对那台设备有同样的权力。

| Harness | 默认 | 收紧方式 |
| --- | --- | --- |
| Codex | sandbox `danger-full-access`，审批策略 `never` | bootstrap 加 `--sandbox workspace-write` 或 `--sandbox read-only` |
| Claude | 传 `--dangerously-skip-permissions` | `harnesses.json` 写 `--no-skip-permissions` |
| dsh | `DSH_PERMISSION_MODE=danger-full-access`，审批 `never` | 没有开关：headless 没人能批准请求 |
| Pi | 没有沙箱和权限确认 | 没有开关 |
| Antigravity | 传 `--dangerously-skip-permissions` | `harnesses.json` 写 `--no-skip-permissions`：要审批的操作在 headless 下被跳过 |

- 权限每轮按服务的当前配置传入，重启后已有话题的下一轮也按新设置。收紧后不会请你确认，无人值守的任务可能因权限不足失败，先用小任务验证（[真实环境验证](testing.md#真实环境验证)）。
- Codex `workspace-write`、`read-only` 下命令可能无法联网；`workspace-write` 下 `checkpoints/` 也可写；在设备上时可写根目录加上设备上的任务目录和检查点镜像目录。
- 以前收紧权限的 `LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 不再读取，设了非默认值时诊断和部署预检失败，见[权限变量让诊断或部署失败](troubleshooting.md#权限变量让诊断或部署失败)。

## 配置 Harness 参数

### 手工加一个 Harness

以 Pi 为例，dsh、Antigravity 同样做：

1. **装好并登录**这个 Harness 的 CLI（[进阶 Harness](#进阶-harnessdshpiantigravity扩展的)），在终端里真实跑通一次。
2. **改 `harnesses.json`**：在数据根下（`./install` 装的是 `<仓库目录名>-data/harnesses.json`，`python3 -B bin/covey_paths.py` 打印数据根）。在 `enabled_harnesses` 里加上它的 ID，需要时写 `harnesses.<ID>.args`：

   ```json
   {"enabled_harnesses": ["codex", "claude", "pi"],
    "harnesses": {"pi": {"args": ["--pi-cmd", "/absolute/pi-runtime/node_modules/.bin/pi"]}}}
   ```

   dsh 还要加 `gateways`，见[最小网关示例](#最小网关示例)。
3. **重启 Bridge**：`python3 -B bin/covey_service.py restart --state-dir "$STATE_DIR"`（`STATE_DIR` 见[排障 · 先找到数据根和 APP_ID](troubleshooting.md#先找到数据根和-app_id)）。重跑 `./install` 不会让它生效：文件路径没变，安装器只说“配置没变”。
4. **确认**：几分钟内启动卡上出现它的模型（目录每 5 分钟刷新）。没出现看 `bridge.log` 的 `launch catalog unavailable harness=<ID> … detail=<原因>`，见[排障](troubleshooting.md#启动卡里缺少某个-harness-的模型)。

以后重跑 `./install` 时，你加的保留（按 Codex、Claude、dsh、Pi、Antigravity 的固定顺序重排，扩展的排在它声明的位置；第一项是新话题的默认 Harness），文件里别的键也保留。

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
| `enabled_harnesses` | 可选：只启用这些，第一项是新话题的默认 Harness；省略时启用全部、默认 Codex。必须非空、不重复、只用支持的 ID |

- 不在名单里（含已下架）的 Harness 键只记一行 `harness config ignores Harnesses not in the roster`，然后忽略。`--state-dir`、`--workdir`、`--profile` 由机器人统一传入，写了也不生效。文件里不写凭据。
- 服务和每个任务进程启动时按路径重新读，文件放持久位置、一次写完整；重启后生效。
- 用了 `enabled_harnesses` 时已有话题保留原 Harness，原后端被禁用后不改由其他后端执行。
- 同一个文件还放 `gateways`（[模型网关](#模型网关codexpi-与-dsh)）和 `spaces`（[配置 · 空间](configuration.md#空间spaces)）。

### 常用参数

Codex 的参数写在 bootstrap 命令上（`setup_harness_bridge.sh` 不接受），其余写进 `harnesses.json` 的 `args`。

| Harness | 参数（默认值） |
| --- | --- |
| Codex（bootstrap） | `--sandbox read-only\|workspace-write\|danger-full-access`（`danger-full-access`）；`--codex-cmd`（`codex`）；`--codex-timeout`（`0`）；`--model`（`gpt-6-astra`）；`--reasoning-effort`（`max`） |
| Claude | `--claude-cmd`（`claude`）；`--claude-timeout`（`0`）；`--model`（`claude-fable-5`）；`--effort`（`xhigh`）；`--no-skip-permissions`；`--claude-login-shell`；`--claude-auto-compact-window`、`--claude-compact-between-turns`（都不设置） |
| dsh | `--dsh-cmd`（`dsh`）；`--dsh-timeout`（`0`）；`--model`（`ali-deepseek-v4.1-flash`，要改成你的网关目录里有的）；`--reasoning-effort`（`high`） |
| Pi | `--pi-cmd`（`pi`）；`--pi-timeout`（`0`）；`--model`（`gpt-6-astra`）；`--reasoning-effort`（`high`） |
| Antigravity | `--agy-cmd`（`agy`）；`--agy-timeout`（`0`）；`--model`（空：agy 自己的默认）；`--reasoning-effort`（空：模型的默认）；`--no-skip-permissions` |

- 超时单位是秒，`0` 或负数不限时长。根任务给出最终回复后还要等子任务、待处理消息和进程清理收尾，设了超时时不超过任务的截止时间（`core/completion.py` 的 `CompletionBudget`），不改变 Harness 自身的模型请求重试。预算参数不影响启动卡的预选。
- 兼容的旧选项（现网 `harnesses.json` 仍能解析）：`--claude-transport stream-json`、`--codex-transport app-server`（只接受这一个值）；`--traex-ppe-env`、`--traex-site`（隐藏，值被忽略）。
- 扩展的 Harness 可以另有读环境变量的参数（见扩展的 README），命令行和 `--harness-config` 优先。以前给参数设默认值的变量不再读取（`core/ignored_env.py` 的 `IGNORED_ENV`，设了启动时各记一行），见[不再读取的变量](legacy.md#不再读取的变量)。

## 适配器结构

给改代码的人。后端契约（`core/harness.py` 的 `HarnessSpec`、`BackendRuntime`、`HarnessCapabilities`）和怎么加后端见[架构与开发说明](architecture.md)；后端不导入 `app/`、`feishu/`、`cards/` 和别的后端（`backends/gateway` 除外），见[硬约束](architecture.md#硬约束)。每个后端的 `bridge.py` 是 Harness 规格、参数和后端类，`sessions.py` 是原生会话位置，其余模块开头的 docstring 写着它管什么。

| 后端 | 代码位置 |
| --- | --- |
| Claude Code（`backends/claude/`） | `runtime.py` 一轮的收发与收尾（`run_stream`、空闲压缩 `compact_only`）；`process.py` CLI 进程和 JSONL 传输（`ClaudeProcess`、`TRANSPORT_FAILURES`）；`control.py` 输入、补充、问答和权限请求（`ClaudeControl`）；`argv.py` 启动参数和环境（`stream_argv`、`turn_env`）；`events.py` 事件（`ClaudeEventAdapter`）；`completion.py` 结果边界；`usage.py` 用量和 TTFT；`models.py` 目录行；`compaction.py`、`rewind.py`、`export.py` 压缩、回退、下载；`policy.py` 官方拒绝；`redaction.py` 脱敏中继 |
| Codex（`backends/codex/`） | `appserver.py` 一轮的开会话、起 turn、收尾（`run_appserver`）；`rpc.py` App Server 进程与 JSON-RPC（`AppServer`、`RpcError`）；`turn.py` 读一个 turn 到结束（`read_turn`、`close_turn`）；`events.py` 事件（`CodexEventAdapter`）；`usage.py` 用量；`gateway.py` 按会话分的 provider；`workspace_reader.py` 只读 MCP；`export.py` 下载 |
| Pi（`backends/pi/`） | `home.py` 每轮私有目录、凭据和预算；`rpc.py` 一轮 RPC 的收发、补充、停止和结束判断（`run_rpc`）；`events.py` 事件 |
| Antigravity（`backends/agy/`） | 只有 `bridge.py`：参数、进程、事件（`AntigravityEventAdapter`）、结果（`stream_outcome`）、模型目录 |
| dsh（`backends/dsh/`） | 只有 `bridge.py`：进程、补丁、事件（`DshEventAdapter`）、结果 |
| 模型网关（`backends/gateway/`，配置在 `core/gateways.py`） | `connection.py` 密钥、目录、请求头、`BUSY_WINDOW`；`relay.py` 中继；`catalog.py` 目录行和繁忙提示（Codex、Pi 共用） |

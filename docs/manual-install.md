# 手工安装

新装用 `./install`（见[安装](install.md)），不用看本文。本文写给要手工控制每一步的人：旧安装脚本怎么用、怎么确定 owner 并启动、装好后怎么验收。飞书应用要有什么、怎么手动配见[飞书应用配置](feishu-app.md)。服务装好以后的日常操作、升级和排障见[运维指南](operations.md)。

`./install`（[安装](install.md)）取代了下面这套；旧脚本 `scripts/setup_harness_bridge.sh` 和 `scripts/bootstrap_harness_bridge.sh` 仍可用、不再推荐（`./install` 和 `deploy_bridge.sh` 也调 bootstrap），只在需要手工控制每一步时用。

## 运行安装脚本

给机器人用一个单独命名的 lark-cli profile（下例为 `my-bot`）。第一次加 `--no-start`，只做本机安装和登录：

```bash
./scripts/setup_harness_bridge.sh --profile my-bot --workdir /absolute/workspace --no-start
```

安装时用到的参数如下，其余（`--app-id`、`--state-dir`、`--skip-login`、`--skip-ext-tools`、`--non-interactive`、`--lark-domains`）见 `--help`：

| 参数 | 含义 |
| --- | --- |
| `--profile NAME` | 机器人应用的 lark-cli profile，默认 `harness` |
| `--workdir DIR` | 主目录（所有话题从这里启动），必须已存在，默认当前目录 |
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

## 配置飞书应用

一般不用自己做：安装脚本在 lark-cli 登录后检查应用，缺的权限、事件、回调和菜单扫码后由浏览器补上并发布（`bin/covey_app_setup.py`）；升级后 Bridge 自己再查，缺了在私聊发「飞书应用待补」卡。应用要有什么、自动补齐怎么做、它做不了时怎么在开发者后台手工配，见[飞书应用配置](feishu-app.md)。

## 启动机器人

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

这一步调用 `scripts/bootstrap_harness_bridge.sh`：把机器人注册为托管服务（清单在 `$COVEY_DATA_HOME/bridge-services/`，状态目录默认 `$COVEY_DATA_HOME/harness-<APP_ID>/`），需要时拉起 Manager，打印服务状态。服务清单记录的是这次运行的脚本所在目录，之后在这个检出里 `git pull` 会改变下次启动的代码；长期使用建议换成固定的 release 目录，见[升级与回滚](deploy.md#升级与回滚)。

**4. 确认就绪。**

```bash
STATE_DIR="$COVEY_DATA_HOME/harness-$APP_ID"
python3 -B bin/covey_service.py status --state-dir "$STATE_DIR"
python3 -B bin/covey_doctor.py --root "$COVEY_DATA_HOME/bridge-services" --runtime-only --json
grep 'ready event_key=' "$STATE_DIR/bridge.log" | tail -n 2
```

三项都满足才算就绪：

- `covey_service.py status` 输出 `"alive": true`，`status.health.status` 为 `ready`；
- `covey_doctor.py --runtime-only` 退出码为 0，这个服务的 `status` 为 `ready`，`events.owned_required_consumers` 为 `true`；
- 最近一次启动后，`bridge.log` 里有 `ready event_key=im.message.receive_v1` 和 `ready event_key=card.action.trigger` 两行；开了菜单订阅还有 `ready event_key=application.bot.menu_v6`。

报告里的 `model_access: not_probed` 表示还没验证过模型请求，由首次验收确认。没注册开机自启的机器重启后机器人不会自动恢复，开机自启要单独注册，见[开机恢复](operations.md#开机恢复)。

## 首次验收

在飞书里依次完成下面五步，全部通过才算安装完成。本地测试不能代替飞书客户端里的点击和回调。卡片和按钮的完整用法见[使用指南](usage.md)。

1. 在与机器人的单聊发送“只告诉我当前工作目录，不修改文件”。消息下出现启动卡；选模型和思考预算，点“开始任务”。核对答案里的目录与主目录（`--workdir`）一致。
2. 在同一话题追问“我刚才问了什么”，确认延续了上下文；再在主聊天另发一条消息，确认它开了一个独立的新话题。
3. 发送“等待 30 秒后报告当前目录，不修改文件”，执行中点任务卡上的“停止”。核对这个任务停下，其他话题不受影响。
4. 对支持原生问答的 Harness（见[支持的 Harness](harnesses.md#支持的-harness)），发送“先问我希望使用中文还是英文，收到回答再继续”。提交回答后，卡片显示明确的状态，任务继续执行。
5. 对每个要用的 Harness 各做一次第 1 步，确认启动卡里它们的模型都能真实调用。

## 安装常见问题

| 现象 | 优先核对 |
| --- | --- |
| `no usable Harness found` | CLI 是否在当前 PATH 里；扩展的运行环境是否通过它自己的检查（见扩展 README） |
| `lark-cli missing and npm is not available`，或装完仍找不到 `lark-cli` | Node/npm 是否可用、npm 全局安装目录是否在 PATH 里；不要因为命令找不到就反复创建应用 |
| `lark-cli auth missing for profile ...` | 去掉 `--skip-login` / `--non-interactive` 重新运行，完成浏览器授权 |
| `profile ... belongs to ..., not --app-id ...` | `--profile` 和 `--app-id` 不是同一个应用 |
| `only non-secret proxy URLs without credentials can be persisted` | 代理 URL 带了用户名或密码，不能写进服务清单；换不带凭据的代理再注册 |
| `--check-runtime` 输出 `"ok": false` | 按 `error` 对照扩展自己的 README 里的检查项 |
| 发消息后没有启动卡 | 两行 `ready event_key=...` 是否都在；订阅、权限、发布是否都在同一个应用上；发消息的人是不是 owner |
| 同事发消息没有回复 | 机器人只服务 owner，给同事部署独立的机器人 |
| 启动卡里缺少某个 Harness 的模型 | 这个 Harness 是否在服务的 PATH 里、是否已用同一系统账号登录；看状态目录 `bridge.log` 里的 `launch catalog unavailable` 日志 |

服务起不来、点按钮没反应、模型调用报权限错误等运行期问题见[排障](troubleshooting.md)。反馈问题时提供 Harness 名称、任务编号、发生时间和脱敏后的错误，不要贴 App Secret 或访问令牌。

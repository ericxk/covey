# Covey

[English](README.en.md)

Covey 在飞书里驱动你自己开发机上的编程 Agent。一个机器人接入 Codex、Claude Code、DeepSeek Harness（dsh）和 Pi；一个飞书话题对应一个 Agent 会话，任务、追问、补充、问答和结果都留在这个话题里。更多 Harness 可以通过[扩展](docs/architecture.md#扩展)加进来。

机器人跑在你自己的机器上，绑定一个飞书用户（主人），用主人自己的模型授权。同事想用自己的主线程、看板和模型授权，各自部署一个；也可以激活一个群，让同事 @ 机器人开话题（团队空间）。

## 界面一览

下面是用演示数据渲染的**卡片示意**：由 Covey 的卡片 JSON 画成，不是真实客户端截图，实际样子以飞书里为准。

<img src="docs/media/screenshots/01-main-thread.png" width="560" alt="私聊里发一句话，主线程开出话题">

私聊里发一句话，主线程开出话题

<img src="docs/media/screenshots/04-launch-card.png" width="560" alt="话题里的启动卡：选模型、思考预算、工作区和运行方式">

话题里的启动卡：选模型、思考预算、工作区和运行方式

<img src="docs/media/screenshots/02-task-card.png" width="560" alt="任务卡：执行过程实时更新，随时补充、换配置或停止">

任务卡：执行过程实时更新，随时补充、换配置或停止

<img src="docs/media/screenshots/03-answer-card.png" width="560" alt="答案卡：结论、产物链接和输入/输出">

答案卡：结论、产物链接和输入/输出

<img src="docs/media/screenshots/05-settings-redaction.png" width="560" alt="设置卡的“脱敏”页：填词即可，替身词自动生成">

设置卡的“脱敏”页：填词即可，替身词自动生成

## 能做什么

- **话题就是会话**：发一条消息开一个话题，选模型、思考预算、工作区和运行方式后开始；话题里的后续消息沿用同一个会话。
- **私聊主线程**：私聊顶层的话交给一个常驻的 Claude Code 会话。能马上答的它直接答，要做的事它开话题去做，结论回到聊天区。
- **卡片上操作**：看执行过程，停止任务，执行中补充要求，回答模型的提问，聊到一半换 Harness 或模型，Claude 话题还能回退一步。
- **结果和产物**：答案单独发成一张卡，登记的产物上传云空间并给出链接。
- **长任务不怕重启**：每个任务在自己的任务进程里运行，机器人重启或升级时任务继续。
- **空间**：私聊和每个绑定的群是一个“空间”。个人空间只有你，有主线程、看板和菜单卡；团队空间是激活的群，同事 @ 机器人开话题；代答空间是代答用的群。设置见[配置](docs/configuration.md#空间spaces)。
- **可选功能**：话题看板和早晚汇总、代答、敏感词脱敏。默认都关，开法见[配置](docs/configuration.md)。脱敏在设置卡里填词，替身词由系统自动生成，话题开了脱敏才会替换，只支持 Claude 的模型。

## 快速开始

前提：机器上已装好并登录了 Claude Code 或 Codex，Linux 或 macOS，Python 3.10+。

```bash
git clone https://github.com/ericxk/covey.git && cd covey
./install        # 终端里出现二维码时，用飞书手机端扫一次
```

装完去飞书私聊机器人发一句话。选项和常见问题见[安装](docs/install.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [安装](docs/install.md) | `./install` 的前提、过程、选项、常见问题 |
| [使用指南](docs/usage.md) | 开任务、补充、回答、换配置、任务卡、主线程、看板、群聊、代答 |
| [配置](docs/configuration.md) | `harnesses.json`、环境变量、可选开关 |
| [运维指南](docs/operations.md) | 服务组成、数据目录、升级、开机恢复、备份、日志 |
| [排障](docs/troubleshooting.md) | 按症状查 |
| [Harness 说明](docs/harnesses.md) | 各 Harness 的参数、能力和限制 |
| [架构与开发说明](docs/architecture.md) | 分层、任务生命周期、锁、状态、投递、扩展机制 |
| [测试与验收](docs/testing.md) | 怎么挑测试、金样、跨版本检查、真实客户端验收 |
| [AGENTS.md](AGENTS.md) | 代码地图和最容易改坏的规则（给改代码的人和 Coding Agent） |

## 扩展

核心不按名字引用任何一个 Harness 之外的东西。部署可以在 `extensions/` 下放扩展包，用 `extensions/enabled` 或环境变量 `COVEY_EXTENSIONS` 启用；扩展通过自带的清单把 Harness、供应商和菜单项并进核心的表。接口和规则见[架构](docs/architecture.md#扩展)。

## 参与

见 [CONTRIBUTING.md](CONTRIBUTING.md)。许可证：[MIT](LICENSE)。

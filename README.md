# Covey

[English](README.en.md)

Covey 在飞书里驱动你自己开发机上的编程 Agent。一个机器人接入 Codex、Claude Code、DeepSeek Harness（dsh）和 Pi；一个飞书话题对应一个 Agent 会话，任务、追问、补充、问答和结果都留在这个话题里。更多 Harness 可以通过[扩展](docs/architecture.md#扩展)加进来。

机器人跑在你自己的机器上，只服务绑定的一个用户，用这个用户自己的模型授权。同事要用，各自部署一个。

> 截图和演示视频还没放：之后用演示数据重拍。

## 能做什么

- **话题就是会话**：发一条消息开一个话题，选模型、思考预算、工作区和运行方式后开始；话题里的后续消息沿用同一个会话。
- **私聊主线程**：私聊顶层的话交给一个常驻的 Claude Code 会话。能马上答的它直接答，要做的事它开话题去做，结论回到聊天区。
- **卡片上操作**：看执行过程，停止任务，执行中补充要求，回答模型的提问，聊到一半换 Harness 或模型，Claude 话题还能回退一步。
- **结果和产物**：答案单独发成一张卡，登记的产物上传云空间并给出链接。
- **长任务不怕重启**：每个任务在自己的任务进程里运行，机器人重启或升级时任务继续。
- **空间**：把话题按用途分组，各有自己的默认工作区和配置。
- **可选功能**：看板和早晚汇总、代答、敏感词脱敏。默认都关，开法见[配置](docs/configuration.md)。

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

---
name: harness-bridge-onboarding
description: Walk a new user step by step through installing their own Feishu bot from this repository, where each topic picks a Harness (Codex, Claude Code, DeepSeek Harness dsh, Pi, plus any Harness an enabled extension adds; Acme models inside Codex, dsh and Pi), model, reasoning effort and workspace on a launch card. Use when someone wants to install, set up or deploy this bot for the first time, set it up on a new machine, or verify a fresh installation end to end. Not for day-to-day operation (docs/operations.md) or using the bot in Feishu (docs/usage.md).
---

# Covey（原 Harness Bridge）安装引导

## 什么时候用

有人要第一次安装**自己的**机器人、换一台机器重装，或从头验收一次新安装。日常运维看 [operations.md](../../docs/operations.md)，飞书里怎么用看 [usage.md](../../docs/usage.md)。

## 怎么做

- 唯一依据是 [install.md](../../docs/install.md)。开始前先读它；下面每一步对应其中一节，细节照那一节做，不要把长段落贴给用户。
- 一次只做一步。检查和命令由你在仓库根目录运行，先说明它做什么；确认这一步的完成条件再往下。
- 只有用户能做的事（浏览器登录、飞书开发者后台、发布版本、模型账号授权）停下来等用户。
- 不索要、不打印、不保存 App Secret、令牌和密码；密钥只由用户输入到 lark-cli 自己的提示或 stdin。
- 不复用别人的飞书应用、lark-cli profile、Harness 登录或状态目录；往已有目录写之前先看里面有什么。
- 求助时只给 Harness、任务号、时间和打码后的报错。

## 步骤

1. **开始前准备**：检查 Bash、Python、git、rsync、npm 等工具，和用户定好 profile 名、默认工作区和持久的数据根。见[开始前准备](../../docs/install.md#开始前准备)。
2. **准备 Harness**：问用户要哪几个 Harness（一个就够），由用户安装、登录，并在每个 CLI 里真实跑通一次请求；登录和之后注册服务要用同一个系统账号和环境。见[准备 Harness](../../docs/install.md#准备-harness)。
3. **扩展 Harness**（只在要用时）：按公司扩展的 README里的“安装”一节搭环境，只在空目录里执行；扩展 README 里的 `--check-runtime` 输出 `"ok": true` 才算完成。
4. **运行安装脚本**：先带 `--no-start` 跑一次，用户在浏览器里建应用、授权。见[运行安装脚本](../../docs/install.md#运行安装脚本)。
5. **配置飞书应用**：用户在开发者后台开机器人能力、订阅事件和回调、开权限并发布版本；停下来等用户确认已发布。见[配置飞书应用](../../docs/install.md#配置飞书应用)。能自动补齐时按[自动补齐飞书应用](../../docs/install.md#自动补齐飞书应用)做。
6. **启动机器人**：设好数据根和代理变量，确认主人身份（需要时用配对码），去掉 `--no-start` 再跑，并做该节列出的就绪检查。见[启动机器人](../../docs/install.md#启动机器人)。
7. **首次验收**：用户在飞书里逐项操作并告诉你看到什么，你同时看 `bridge.log`；该节清单全部通过才算装好。见[首次验收](../../docs/install.md#首次验收)。
8. **出了问题**：先查[安装常见问题](../../docs/install.md#安装常见问题)，运行期问题看 [operations.md](../../docs/operations.md)。

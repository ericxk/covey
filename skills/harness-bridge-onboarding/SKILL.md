---
name: harness-bridge-onboarding
description: Walk a new user through installing their own Covey Feishu bot from this repository with ./install (one QR scan), where each topic runs a Harness (Codex, Claude Code, DeepSeek Harness dsh, Pi, Antigravity agy, plus any Harness an enabled extension adds; model gateways a deployment configures) with a model, reasoning effort and workspace. Use when someone wants to install the bot for the first time, set it up on a new machine, or verify a fresh installation end to end. Not for day-to-day operation (docs/operations.md) or using the bot in Feishu (docs/usage.md).
---

# Covey 安装引导

## 什么时候用

有人要第一次安装**自己的**机器人、换一台机器重装，或从头验收一次新安装。日常运维看 [operations.md](../../docs/operations.md)，飞书里怎么用看 [usage.md](../../docs/usage.md)。

## 怎么做

- 唯一依据是 [install.md](../../docs/install.md)。开始前先读它；下面每一步对应其中一节，细节照那一节做，不要把长段落贴给用户。
- 一次只做一步。命令由你在仓库根目录运行，先说明它做什么；确认这一步的完成条件再往下。
- 只有用户能做的事（用飞书手机端扫码、登录 Claude Code 或 Codex）停下来等用户。
- 不索要、不打印、不保存 App Secret、令牌和密码。
- 不复用别人的飞书应用、lark-cli profile、Harness 登录或数据根；往已有目录写之前先看里面有什么。
- 求助时只给 Harness、任务号、时间和打码后的报错。

## 步骤

1. **查前提**：Linux 或 macOS、Python 3.10+；Claude Code 或 Codex 至少一个已登录，并在终端里真实跑通一次（`claude -p "ok"` 或 `codex exec --skip-git-repo-check "ok"`）。用户有公司或团队给的扩展库时，改照扩展库 README 的安装一节做（它会放好扩展再运行 `./install`），下面第 2 步换成它的命令。见[前提](../../docs/install.md#前提)。
2. **运行 `./install`**：在仓库根目录运行。需要代理的机器先 `export HTTPS_PROXY=http://主机:端口`（不能带用户名密码）。安装器每步打印一行标题，见[它做了什么](../../docs/install.md#它做了什么)和[选项](../../docs/install.md#选项)。
3. **等用户扫码**：终端里出现二维码时，请用户用飞书手机端扫一次（扫码的人就是机器人的主人）；之后安装器自己建应用、授权、补配置、注册服务。最后一行是“完成。去飞书私聊机器人「…」发一句话”；有“还差：……”时照它说的做。
4. **发第一句话并验收**：请用户私聊机器人发「只告诉我当前工作目录，不修改文件」，再按[第一个回答](../../docs/install.md#第一个回答)的五步验收逐项做，用户告诉你看到什么，你同时看状态目录的 `bridge.log`（位置见[排障 · 先找到数据根和 APP_ID](../../docs/troubleshooting.md#先找到数据根和-app_id)）。全部通过才算装好。
5. **出了问题**：安装时的问题查[出问题](../../docs/install.md#出问题)和数据根的 `install-logs/`；装好后的问题按[排障 · 按现象找](../../docs/troubleshooting.md#按现象找)查。装好以后要打开更多功能，见[配置 · 开关速查](../../docs/configuration.md#开关速查)。

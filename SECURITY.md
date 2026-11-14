# 安全政策 / Security Policy

[中文](#中文) | [English](#english)

## 中文

### 怎么私下报告漏洞

请不要在公开 Issue、PR 或讨论里写漏洞细节。用 GitHub 的私有安全通告报告：打开本仓库的 **Security** 标签，点 **Report a vulnerability**（<https://github.com/ericxk/covey/security/advisories/new>）。

请写清楚：影响什么、怎么复现、你测试的提交号（`git rev-parse HEAD`）。不要附真实的令牌、聊天内容或飞书 id，用一看就是假的值。收到后会先确认，修好并合进 `main` 之后再公开说明。

### 支持的版本

只维护 `main` 分支的最新提交。还没有发布版本；有问题请先在最新的 `main` 上复现。

### 威胁模型

Covey 是一个跑在你自己机器上的个人工具，不是多租户服务。

- **机器人以主人的权限跑 Agent**：任务进程是机器上的普通进程，Agent 能读写主人账号能访问的文件、执行命令、使用主人登录的模型账号。Covey 不提供沙箱；要隔离请用专用机器、专用账号或容器，并设置工作区和运行方式。
- **只服务绑定的主人**：私聊只认绑定的主人，配对只在私聊里进行。群里只认 @ 了机器人的消息；发送者是主人，或者这个群已被主人激活。外部群不自动激活。
- **群里其他人**：已激活的群里，任何人点卡片按钮可以做该卡片允许的事（如补充、停止），但工作区管理、目录选择和菜单只给主人。群成员发的话会进入 Agent 的输入，所以只激活你信任的群。
- **凭据不进仓库**：账号登录、令牌、SSH 配置、Agent 的授权文件都留在你的机器和数据根里，不写进仓库、文档、测试和提交说明；个人环境靠配置文件和 `COVEY_*` 环境变量传入。
- **隐私检查**：`scripts/privacy_check.py` 在 CI 里扫描要公开的文件，查飞书 id、内网地址、真实邮箱、令牌形状和个人路径。它能挡住常见的误提交，但不是保证；提交前请自己再看一遍。

不在范围内：Agent 本身（Claude Code、Codex 等）的漏洞，请报告给它们各自的厂商；主人自己授权 Agent 做的操作。

## English

### Reporting a vulnerability privately

Please do not describe a vulnerability in a public issue, pull request or discussion. Use GitHub's private Security Advisories: open the **Security** tab of this repository and choose **Report a vulnerability** (<https://github.com/ericxk/covey/security/advisories/new>).

Say what is affected, how to reproduce it and the commit you tested (`git rev-parse HEAD`). Do not attach real tokens, chat content or Feishu ids; use obviously fake values. We confirm first and disclose after the fix is merged into `main`.

### Supported versions

Only the latest commit on `main`. There are no tagged releases yet; reproduce the problem on the latest `main` first.

### Threat model

Covey is a personal tool that runs on your own machine, not a multi-tenant service.

- **The bot runs the agent with the owner's permissions.** A task runs as an ordinary process: the agent can read and write whatever the owner's account can, run commands and use the model accounts the owner is logged in to. Covey is not a sandbox; for isolation use a dedicated machine, account or container, and set the workspace and run mode.
- **It serves only the bound owner.** A direct chat accepts only the bound owner, and pairing happens only there. In a group only messages that @-mention the bot count, and only if the sender is the owner or the group was activated by the owner. External groups are never activated automatically.
- **Other people in a group.** In an activated group anyone can press the buttons a card allows (add a note, stop), but workspace management, the directory picker and the menu are owner-only. What group members write becomes agent input, so activate only groups you trust.
- **Credentials stay out of the repository.** Logins, tokens, SSH configuration and the agent's auth files stay on your machine and in your data root, never in the repository, docs, tests or commit messages. Personal settings come from config files and `COVEY_*` variables.
- **Privacy check.** `scripts/privacy_check.py` scans the files to be published for Feishu ids, internal addresses, real emails, token shapes and personal paths. It catches common slips but is no guarantee; review your own diff too.

Out of scope: vulnerabilities in the agents themselves (Claude Code, Codex, ...), which belong with their vendors, and actions the owner has authorized the agent to take.

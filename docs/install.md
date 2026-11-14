# 安装

一条命令、扫一次码、发一句话，5 分钟内拿到第一个回答。Linux 和 macOS。

## 前提

这台机器上已装好并登录了 Claude Code 或 Codex（任一个）。怎么装、怎么登录见[各 Harness 的官方说明](harnesses.md#安装和登录-harness)；安装器不教、也不替你装。另外要 Python 3.10+；没有 `lark-cli` 时安装器会用 npm 装它（没有 npm 时它会告诉你去装 Node.js）。

## 安装

```bash
git clone https://github.com/OWNER/covey.git && cd covey
./install
```

终端里出现一个二维码时，用飞书手机端扫一次（扫码的人就是机器人的主人，不用填 open_id、不用配对）。之后什么都不用点，最后一行告诉你去飞书私聊机器人发一句话。

## 它做了什么

每步一行标题，只在有事时多说一句：

1. **检查环境和目录**：数据根（默认是仓库同级的 `<仓库目录名>-data`）、默认工作区（当前目录；在仓库里运行时用 `<数据根>/workspace`）、临时目录、浏览器和 `lark-cli` 的安装位置都有默认值，自动建好，不写系统盘。
2. **准备 `lark-cli`**：没有就装；Skills 默认不同步（要用加 `--skills`）。
3. **检查 Harness**：对 Claude Code 和 Codex 各真实调用一次模型。没登录、网络不通会分别说明；没调通的不启用，写进 `harnesses.json` 的只有调通的。网络不通时问一次代理地址，通过后代理和飞书域名的 `NO_PROXY` 一起写进服务环境。
4. **飞书应用**：先准备一个无头浏览器（用机器上已有的 Chrome/Chromium，没有就下载 Chromium，显示进度，60 秒没进展自动换 npmmirror 镜像；Debian/Ubuntu 缺系统库时，有 root 用 `playwright install-deps`，没有 root 自动下载需要的 deb 解到数据根下，只给浏览器进程用）。然后扫一次码，在同一个会话里建应用、授权、在开发者后台补回调、事件、菜单和权限、发布，再读回核对。浏览器装在数据根下，主人的网页登录只在本次运行的临时目录里，结束就删（`--keep-login` 留下）。
5. **注册并启动服务**：托管服务启动，并注册开机自启（Linux 用 systemd 用户服务，macOS 用 launchd；没有 systemd 但有 `crontab` 时往 crontab 加一行 `@reboot`，每个服务根一行（标记带服务根的短哈希）：同一服务根重复运行不重复加，不同服务根的 Bridge 各留各的，`--no-autostart` 跳过；都没有就是 Manager 方式，开机不会自启，安装器会说明）。
6. **等待就绪**：诊断通过后打印「去飞书私聊机器人「Covey」发一句话」。

## 选项

| 选项 | 含义 |
| --- | --- |
| `--reconfigure` | 重来：重新建应用、授权、补配置并重注册服务。不加时，已有的应用、授权、配置、服务都复用，重复运行是安全的 |
| `--skills` | 同时同步 `skills/` 到 Codex 和 Claude 的 Skills 目录 |
| `--non-interactive` | 不提问；缺什么就报什么退出 |
| `--data-home DIR` | 数据根 |
| `--workdir DIR` | 默认工作区 |
| `--keep-login` | 不删浏览器里主人的网页登录（之后补配置可用 `covey_app_setup.py apply --login-from`） |
| `--no-browser` | 不用无头浏览器：每个链接画成二维码，用手机依次打开（浏览器装不上时安装器会自动这样做，并说明还差什么） |
| `--profile`、`--app-name` | `lark-cli` profile 名（默认 `covey`）、新应用的名称（默认 `Covey`） |
| `--no-start` | 只准备到飞书应用为止，不注册、不启动服务 |

## 第一个回答

在飞书里私聊机器人，发「只告诉我当前工作目录，不修改文件」：消息下出现启动卡，点「开始任务」，答案里的目录就是默认工作区。完整验收（追问、停止、问答）见[旧安装脚本与手动配置](operations.md#首次验收)。

## 出问题

| 现象 | 做法 |
| --- | --- |
| 「没有能调通的 Harness」 | 先在终端里登录 Claude Code 或 Codex，并确认 `claude -p "ok"` 或 `codex exec "ok"` 有回答 |
| Harness 探测像网络不通 | 先 `export HTTPS_PROXY=http://主机:端口` 再 `./install`；代理地址不能带用户名密码 |
| 浏览器装不上、下载太慢 | 安装器自动改用手机扫码，也可以一开始就 `./install --no-browser`：每个链接画成二维码和 PNG，一步一步扫（建应用、授权、补回调和事件订阅，后两者一个链接一步，共 3 步）；非交互（没有终端）时把链接都列出来，扫完后重新运行 ./install 或等几分钟，机器人缺订阅时每 20 秒重试一次。机器上有 Chrome/Chromium 时会直接用它；其他发行版缺系统库用系统的包管理器装 |
| 「还差：……」 | 安装完成但飞书应用没补全（手机扫码时菜单做不了）：不影响收发消息，发消息给机器人后点「飞书应用待补」卡的「扫码自动补齐」，或重新运行 `./install` |
| 其他 | 日志在数据根的 `install-logs/`；服务运行期问题见[排障](troubleshooting.md) |

## 进阶

- Harness 参数、dsh、Pi、模型网关：[Harness 说明](harnesses.md)
- 群聊、代答、看板等可选功能：[使用指南](usage.md)、[配置](configuration.md)
- 重启、升级、开机恢复、备份、手动配飞书应用、旧安装脚本：[运维指南](operations.md)

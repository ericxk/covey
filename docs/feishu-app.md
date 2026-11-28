# 飞书应用配置

每个机器人背后是一个飞书自建应用。`./install` 会建好应用、扫一次码后替你补齐它要的配置；升级以后 Bridge 每天自己再查一遍，缺了就在私聊发「飞书应用待补」卡，点一下就补。所以多数人不用打开开发者后台。

本文写给想知道应用要有什么、自动补齐怎么做的、它做不了时怎么手工配的人。安装流程见[安装](install.md)，不用 `./install` 的手工安装见[手工安装](manual-install.md)，缺权限、菜单不出来按[排障](troubleshooting.md)查。

## 应用要有什么

清单在 `core/app_manifest.py` 的 `required`，按本部署的开关算；自动补齐和 `bin/covey_app_setup.py check` 都按它比。

**机器人能力**：启用，可用范围包含主人自己。

**事件与回调**（都选长连接接收，事件连接由 `lark-cli event consume` 建立，不需要公网回调地址）：

| 订阅 | 类型 | 用途 | 什么时候要 |
| --- | --- | --- | --- |
| `im.message.receive_v1` | 事件 | 收消息（含斜杠命令） | 总要 |
| `card.action.trigger` | 回调 | 所有卡片按钮和选择 | 总要 |
| `application.bot.menu_v6` | 事件 | 旧版底部自定义菜单的点击 | 仅在应用已启用旧版底部自定义菜单时需要；未启用时不要求 |
| `im.message.message_read_v1` | 事件 | 话题看过没有（已读） | `COVEY_PINNED_TOPICS` 没关时要（默认开） |

**权限**：

| 权限 | 用途 |
| --- | --- |
| `im:message.p2p_msg:readonly` | 收私聊消息 |
| `im:message.group_at_msg:readonly` | 收群里 @ 机器人的消息 |
| `im:message:readonly` | 读消息和引用 |
| `im:message:send_as_bot` | 发消息 |
| `im:message` | 撤回消息 |
| `im:message:update` | 改卡片 |
| `cardkit:card:write` | 任务卡实时更新 |
| `im:resource` | 收发图片和文件 |
| `im:chat:read` | 读群信息（群里激活时看是否外部群） |
| `im:message.reactions:write_only` | 在你的消息上加表情 |
| `drive:file:upload`、`space:folder:create` | 产物上传云空间 |
| `docs:permission.member:create` | 产物文件夹给你权限 |
| `application:application:self_manage` | 检查应用配置（自动补齐靠它读应用详情） |
| `im:chat.tabs:write_only` | 私聊顶栏的「工作台」标签（网页工作台一键部署以机器人身份建，见 [web/README.md 的「部署」](../web/README.md#部署)）；免审核，默认就要。建和更新标签的响应里已带会话全部标签，所以不要 `im:chat.tabs:read` |
| `im:message.pins:read`、`im:message.pins:write_only` | 置顶看板；`COVEY_PINNED_TOPICS` 或 `COVEY_PINNED_OVERVIEW` 没关时要（默认开） |
| `im:chat.members:read` | 群聊免@ 选人；`COVEY_GROUP_ALL_MESSAGES=1` 时要 |
| `im:message.group_msg` | 群聊免@ 收群里全部消息；`COVEY_GROUP_ALL_MESSAGES=1` 时要，要租户管理员审核 |

`lark-cli config init --new` 的应用模板带了其中大部分。某个订阅具体要哪些权限，以 `lark-cli event schema <订阅名>` 的 `Required Scopes` 为准。不用群聊免@ 就不要申请 `im:message.group_msg`，见[在群里用](usage.md#在群里用)。

**斜杠命令（默认，官方 OpenAPI 自动注册，无需浏览器与开发者后台）**：`./install` 与 Bridge 的日常检查会以机器人身份调用 `lark-cli application +slash-command-create --as bot --force`（对应 `POST /open-apis/application/v7/app_slash_commands`，使用 `lark-cli config init --new` 默认自带的 `application:app_slash_command:read/write` 权限，定义在 `feishu/slash_commands.py`），在输入框打 `/` 即可呼出（客户端缓存约 5 分钟生效，也可直接手打英文命令或中文别名）：

| 斜杠命令 | 中文别名 | 对应菜单键 | 什么时候注册 |
| --- | --- | --- | --- |
| `/usage` | `/用量` | `weekly_usage` | 总要 |
| `/board` | `/看板` | `digest_board` | 每个部署都要 |
| `/proxy` | `/代答` | `proxy_settings` | 启用的扩展有代答发送器 |
| `/settings` | `/设置` | `bridge_settings` | 总要 |

**旧版底部自定义菜单（可选兼容）**：不再要求配置。已经配过底部悬浮菜单的老部署继续完全可用（动作“推送事件”，事件 key 同上表，以及以前单独配过的 `main_thread_settings`；`manage_workspaces`、`redaction_dashboard`、`manage_groups` 三项已移到设置卡的页签，补齐时会把它们从应用菜单里去掉，客户端缓存的旧菜单点它们只回一句话）；新部署直接用斜杠命令即可，无需打开开发者后台或做桌面设备认证。

- “设置”是一张卡上几个标签：配置（设了 `COVEY_BUTLER=0` 时没有管家那一段）、工作区、群聊（开了节点再加设备）、诊断（下半是“能做什么”），见[机器人菜单](usage.md#机器人菜单)。斜杠命令和菜单都只对主人有反应，不开话题。

## 自动补齐飞书应用

Bridge 自己检查上面的清单。缺了就在主人私聊里发一张「飞书应用待补」卡；主人点「扫码自动补齐」后，Bridge 用浏览器在开发者后台补上并发布新版本，再用官方接口回读核对。

**检查。** 启动 90 秒后一次、之后每天一次（`app/app_setup.py`）：以机器人身份读应用和它线上版本的详情（`feishu/app_config.py`），比出缺的项。

- 读这两个接口要应用有 `application:application:self_manage`；没有时卡上只列这一项，补上后下次检查才看得到其余的。
- 同一组缺项只发一次卡（`<state>/app-setup/state.json`）；点「不管了」后，缺项变了才再发。主人还没和机器人私聊过时不发。
- 要租户管理员审核的权限（`im:message.group_msg`）单列在卡上、默认不申请，只缺它时不发卡；点「连同需审核的一起申请」才一起提交。
- 老部署升级后清单多了 `im:chat.tabs:write_only`：缺这一项就发一张待补卡（同一组缺项只发一次，补上后不再提示）；点「扫码自动补齐」只加这一项权限（免审核，不提交管理员审核）。不用网页工作台的部署也会提示，点「不管了」即可，不影响别的功能。
- 「网页应用」能力不在默认清单里：它只为把网页工作台固定到飞书左侧主导航，没有开放接口，Bridge 也不替你在后台点（要建版本并发布，可能要管理员审核）。要用时在开发者后台手工开，步骤见 [web/README.md 的「部署」](../web/README.md#部署)。
- 只增不删：应用里别的权限、事件和菜单项都不动；唯一的删是旧菜单项「仓库」「脱敏」「群聊」（`manage_workspaces`、`redaction_dashboard`、`manage_groups`）：它们还在应用菜单里时，卡上列“菜单旧项……（已移到设置卡）”，补齐时去掉并发布新版本。一级菜单放不下的菜单项不补，卡上说明。
- 设 `COVEY_APP_CHECK=0` 不检查、不发卡。

**补齐。** 点「扫码自动补齐」后（只认主人，同一时间只跑一次），Bridge 在 `<state>/app-setup/runs/<时间>/`（0700）里用装了 Playwright 的 Python（`COVEY_PLAYWRIGHT_PYTHON`，默认 `<数据根>/proxy-web/venv/bin/python`；没有时用 `scripts/install_console_browser.sh` 装一个）起 `bin/covey_app_console.py`：

1. 登录：有代答的网页端登录（`<state>/proxy/web/profile`）就复制一份来用；没有或失效时把开发者后台的登录二维码回复在卡下面，过期自动换新的。
2. 读后台配置；后台有一个不是 Bridge 建的未发布版本时停下，请主人先处理。
3. 权限与事件：读应用清单，加上缺的权限、事件和回调写回，再读一次核对。
4. 菜单：按原样保存已有的菜单（图标、名字都在），去掉上面那三项旧菜单项，再加上缺的菜单项（飞书内置线性图标）。
5. 发布：建一个比现有最高版本号大一的版本，可用范围照线上版本，申请发布；要管理员审核时卡上写“已提交，等租户管理员审核”。

结束后用官方接口再查一遍，卡上逐项写已生效、仍然缺；失败时写卡在哪一步和后台的报错原文，并回复那一刻的页面截图。运行目录（登录的副本、二维码、截图）在运行结束时整个删掉，不留登录。

**在终端里做。** `./install` 在授权后跑同样的检查和补齐（装机时放在同一个浏览器会话里，只扫一次码），二维码画在终端里，这一步跳不过（只有[手工安装](manual-install.md)的 `scripts/setup_harness_bridge.sh` 有 `--skip-app-setup`）；装不了浏览器时，事件和回调给一个手机扫码的一键链接，菜单和权限留给之后的「飞书应用待补」卡。手动：

```bash
python3 -B bin/covey_app_setup.py check --profile my-bot          # 退出码 0 不缺，3 有要补的，1 读不到
python3 -B bin/covey_app_setup.py apply --profile my-bot          # 终端扫码；--review 连同需审核的权限
scripts/install_console_browser.sh                                 # 装浏览器环境，最后一行是解释器路径
```

部署脚本也报应用缺什么（`deploy_bridge.sh` 结果里的 `post.app_config`），不算部署失败。

**限制。** 写开发者后台用的是后台网页自己调的接口，不是公开 API，后台改版可能让某一步失败；失败时照下一节手工补。企业租户在新机器上第一次网页登录可能要在桌面端确认设备，未登记的设备可能被条件访问拦下，这时在能登录的机器上用 `covey_app_setup.py apply --app-id <应用>` 补（那台机器上要有这个应用的 lark-cli profile）。装不了 Chromium 的系统库时（要 root：`python -m playwright install-deps chromium`），检查照常、卡上给手工做法。

## 在开发者后台手动配置

自动补齐做不了时（机器上装不了 Chromium、租户不让这台机器登录网页等），在[飞书开发者后台](https://open.feishu.cn/app)打开这个机器人的应用，照[应用要有什么](#应用要有什么)手工做：

1. 启用机器人能力，可用范围包含自己。
2. 在“事件与回调”里加上表中的订阅，都选长连接接收。
3. 在“机器人”页启用自定义菜单，按菜单表配置；图标在“添加图标 → 上传图标”里传表中的文件，或选同名的内置图标。
4. 申请权限表里的应用权限；发消息、更新卡片、上传文件时接口报缺权限的，按报错补齐。用户登录授权不能代替应用权限和发布。
5. 发布一个版本（订阅、权限和菜单都要发布后才生效）。

能收到文字消息不代表卡片权限已经齐全，要以[首次验收](manual-install.md#首次验收)为准。改完可以跑一次 `python3 -B bin/covey_app_setup.py check --profile <profile>` 确认不缺。

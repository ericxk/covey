# 飞书应用配置

每个机器人背后是一个飞书自建应用。`./install` 会建好应用、扫一次码后替你补齐它要的配置；升级以后 Bridge 每天自己再查一遍，缺了就在私聊发「飞书应用待补」卡，点一下就补。所以多数人不用打开开发者后台。

本文写给想知道应用要有什么、自动补齐怎么做的、它做不了时怎么手工配的人。安装流程见[安装](install.md)，不用 `./install` 的手工安装见[手工安装](manual-install.md)，缺权限、菜单不出来按[排障](troubleshooting.md)查。

## 应用要有什么

清单在 `core/app_manifest.py` 的 `required`，按本部署的开关算；自动补齐和 `bin/covey_app_setup.py check` 都按它比。

**机器人能力**：启用，可用范围包含主人自己。

**事件与回调**（都选长连接接收，事件连接由 `lark-cli event consume` 建立，不需要公网回调地址）：

| 订阅 | 类型 | 用途 | 什么时候要 |
| --- | --- | --- | --- |
| `im.message.receive_v1` | 事件 | 收消息 | 总要 |
| `card.action.trigger` | 回调 | 所有卡片按钮和选择 | 总要 |
| `application.bot.menu_v6` | 事件 | 机器人菜单的点击 | 总要；不开时机器人照常工作，只是菜单点了没反应 |
| `im.message.message_read_v1` | 事件 | 话题看过没有（已读） | `COVEY_PINNED_TOPICS=1` |

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
| `im:message.pins:read`、`im:message.pins:write_only` | 置顶看板；`COVEY_PINNED_TOPICS=1` 或 `COVEY_PINNED_OVERVIEW=1` 时要 |
| `im:chat.members:read` | 群聊免@ 选人；`COVEY_GROUP_ALL_MESSAGES=1` 时要 |
| `im:message.group_msg` | 群聊免@ 收群里全部消息；`COVEY_GROUP_ALL_MESSAGES=1` 时要，要租户管理员审核 |

`lark-cli config init --new` 的应用模板带了其中大部分。某个订阅具体要哪些权限，以 `lark-cli event schema <订阅名>` 的 `Required Scopes` 为准。不用群聊免@ 就不要申请 `im:message.group_msg`，见[在群里用](usage.md#在群里用)。

**机器人菜单**：启用自定义菜单，展示形式选“悬浮菜单”（浮在输入框上方，不挡输入），都是一级菜单（飞书最多 5 个），动作都选“推送事件”，事件 key 一字不差（机器人按它认菜单项）：

| 菜单 | 事件 key | 图标（自动补齐用飞书内置线性图标；手工配可以上传右边的文件） | 什么时候要 |
| --- | --- | --- | --- |
| 用量 | `weekly_usage` | `insert-chart_outlined`；`docs/media/menu-icons/usage.png` | 总要 |
| 看板 | `digest_board` | `bitablekanban_outlined` | 设了 `COVEY_BOARD=1`（`./install` 写）或 `COVEY_DIGEST_TIMES` |
| 代答 | `proxy_settings` | `robot_outlined`；`docs/media/menu-icons/proxy.png` | 启用的扩展有代答发送器 |
| 设置 | `bridge_settings` | `setting_outlined`；`docs/media/menu-icons/settings.png` | 总要 |

- “设置”是一张卡上几个标签：新话题、管家（设了 `COVEY_BUTLER=0` 时没有）、工作区、脱敏、群聊（开了节点再加设备），见[机器人菜单](usage.md#机器人菜单)。菜单只对主人有反应，结果发在主人和机器人的私聊里。
- 以前单独配的菜单项照旧能点：`manage_workspaces`（工作区）、`redaction_dashboard`（脱敏）、`manage_groups`（群聊，图标 `docs/media/menu-icons/groups.png`）、`main_thread_settings`。它们算进 5 项上限，自动补齐不删也不换。
- 上传的图标飞书 7.22 以上显示；菜单补上后约 5 分钟出现在输入框上方。

## 自动补齐飞书应用

Bridge 自己检查上面的清单。缺了就在主人私聊里发一张「飞书应用待补」卡；主人点「扫码自动补齐」后，Bridge 用浏览器在开发者后台补上并发布新版本，再用官方接口回读核对。

**检查。** 启动 90 秒后一次、之后每天一次（`app/app_setup.py`）：以机器人身份读应用和它线上版本的详情（`feishu/app_config.py`），比出缺的项。

- 读这两个接口要应用有 `application:application:self_manage`；没有时卡上只列这一项，补上后下次检查才看得到其余的。
- 同一组缺项只发一次卡（`<state>/app-setup/state.json`）；点「不管了」后，缺项变了才再发。主人还没和机器人私聊过时不发。
- 要租户管理员审核的权限（`im:message.group_msg`）单列在卡上、默认不申请，只缺它时不发卡；点「连同需审核的一起申请」才一起提交。
- 只增不删：应用里别的权限、事件和菜单项都不动。一级菜单放不下的菜单项不补，卡上说明。
- 设 `COVEY_APP_CHECK=0` 不检查、不发卡。

**补齐。** 点「扫码自动补齐」后（只认主人，同一时间只跑一次），Bridge 在 `<state>/app-setup/runs/<时间>/`（0700）里用装了 Playwright 的 Python（`COVEY_PLAYWRIGHT_PYTHON`，默认 `<数据根>/proxy-web/venv/bin/python`；没有时用 `scripts/install_console_browser.sh` 装一个）起 `bin/covey_app_console.py`：

1. 登录：有代答的网页端登录（`<state>/proxy/web/profile`）就复制一份来用；没有或失效时把开发者后台的登录二维码回复在卡下面，过期自动换新的。
2. 读后台配置；后台有一个不是 Bridge 建的未发布版本时停下，请主人先处理。
3. 权限与事件：读应用清单，加上缺的权限、事件和回调写回，再读一次核对。
4. 菜单：按原样保存已有的菜单（图标、名字都在），再加上缺的菜单项（飞书内置线性图标）。
5. 发布：建一个比现有最高版本号大一的版本，可用范围照线上版本，申请发布；要管理员审核时卡上写“已提交，等租户管理员审核”。

结束后用官方接口再查一遍，卡上逐项写已生效、仍然缺；失败时写卡在哪一步和后台的报错原文，并回复那一刻的页面截图。运行目录（登录的副本、二维码、截图）在运行结束时整个删掉，不留登录。

**在终端里做。** `./install` 在授权后跑同样的检查和补齐（装机时放在同一个浏览器会话里，只扫一次码），二维码画在终端里（`--skip-app-setup` 跳过）；装不了浏览器时，事件和回调给一个手机扫码的一键链接，菜单和权限留给之后的「飞书应用待补」卡。手动：

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

# Covey 网页工作台

Covey 的配套网页应用：私聊里的专项、话题、每一轮和等你回答的问题都在网页上看得到，也能在网页上说话、答题、停话题、开新话题、改设置。网页是主工作台，飞书管提醒和快速回复，两边读写同一份记录。能做什么、Covey 怎么和它同步，见 [docs/web.md](../docs/web.md)。

## 部署

| 你的情况 | 怎么部署 |
| --- | --- |
| 飞书企业开通了妙搭（能建全栈应用） | **一键部署到你自己的妙搭**（推荐）：网址、加密、飞书登录都由妙搭提供，人在外面用手机飞书点开就能用。见 [deploy/miaoda/README.md](deploy/miaoda/README.md) |
| 没有妙搭（个人版飞书、Lark 国际版），或想让数据只留在自己机器上 | **自托管**：网页和 Covey 跑在同一台机器上，由 Covey 的 Manager 看护，飞书私聊里点「允许」登录。本机直接打开；别的设备可以让网页进程直接开公网 HTTPS（自签证书或自己的域名和证书，`./install --web-public`），也可以经 ssh 转发、Tailscale 或自己的 https 反向代理打开。见 [deploy/selfhost/README.md](deploy/selfhost/README.md) |

一键部署到妙搭（先只看不做，确认后真做）：

```bash
python3 web/deploy/miaoda/deploy.py --covey-state /absolute/path/to/covey-state --dry-run
python3 web/deploy/miaoda/deploy.py --covey-state /absolute/path/to/covey-state --yes
```

部署脚本最后一步会在你和机器人的私聊顶栏建「工作台」标签（飞书内嵌打开，要机器人有 `im:chat.tabs:write_only` 权限）。老部署补建，或当时缺权限、还没私聊过没做成时，单独补一遍：

```bash
python3 web/deploy/miaoda/deploy.py --covey-state /absolute/path/to/covey-state --only-chat-tab --yes
```

建好的标签自动排在「消息」后面。要把工作台也固定到飞书左侧主导航，需在 Covey 机器人那个飞书应用的开发者后台手工做：应用能力里添加「网页应用」，桌面端主页和移动端主页都填网页地址（不要加 `lk_jump_to_browser`），再创建版本并发布；这几步没有开放接口，部署脚本结尾会打印，详见 [deploy/miaoda/README.md](deploy/miaoda/README.md#固定到飞书主导航)。

自托管：装 Covey 时加 `./install --web`（要公网直连加 `./install --web-public <IP或域名>`）一步做完；也可以手动构建、备好密钥和配置，再按打印的开关改 Covey 的服务清单：

```bash
bash web/deploy/selfhost/build.sh /absolute/path/to/covey-web
python3 -B -m covey.service.web_selfhost --dir /absolute/path/to/covey-web/dist-selfhost
```

## 谁能看

- 网页只给主人用。妙搭版：表从建好那一刻起就只有主人能读（发件和附件只有主人能写），应用的可见范围设成只有你。自托管版：页面接口都要会话，会话只能靠飞书私聊里点「允许」换来，伪造的平台身份头一律丢掉。
- Covey 推数据、取发件箱走开放接口，密钥只存在本机的密钥文件里（权限 600），不进日志和配置以外的地方。自托管版的开放接口只收本机直接来的请求。
- 日志默认不记请求体和返回体（`.env.example` 的 `LOG_REQUEST_BODY`、`LOG_RESPONSE_BODY`），对话内容不进日志。

## 本地开发和检查

要 Node 22 及以上。在 `web/` 下：

```bash
npm ci
npm run lint                     # eslint、类型检查、stylelint
npm run test:collab              # 单测：scripts/*.test.mjs，package.json 里 test: 开头的都是（见下）
npm run build:server             # 妙搭版服务端（NestJS）构建到 dist/server
MIAODA_APP_TYPE=3 npm run build:client   # 妙搭版前端（Vite + React）构建到 dist/client，和妙搭上的构建一致
npm run build:selfhost           # 自托管版：服务端和前端构建到 dist-selfhost/
```

**`.env` 和头像**

- 仓库里提交的是 `.env.example`（三行日志配置，没有密钥）；`.env` 在 `.gitignore` 里，各人本机的覆盖写在那里。`npm run dev` 和 `prune-smart` 先读 `.env`，没有的项用 `.env.example`；`scripts/build.sh` 和自托管的 `build.sh` 构建时，没有 `.env` 就用 `.env.example` 生成，妙搭一键部署把 `.env.example` 当成应用仓库的 `.env`。所以不用手动建 `.env`，行为和原来提交 `.env` 时一样。
- 头像只有一份，在核心的 `covey/assets/avatars/`（核心运行时从那里读，上传给飞书）。`client/src/cv/avatars/` 是 `scripts/sync-avatars.mjs` 从它拷来的，在 `.gitignore` 里，不要手改。`npm run dev`、`dev:client`、`build`、`build:client`、`build:selfhost:client` 的 `pre` 钩子、自托管的 `build.sh` 和妙搭的 `deploy.py` 都会先拷一遍；只拷 `web/` 出去（没有 `covey/`）时，目录已经在就不动，不在就报错。第一次在新检出里直接跑 `vite` 或 `tsc` 之前，先 `npm run sync:avatars`。

单测一共 43 组：工作台 40 组（`test:graph` 到 `test:status`，加 `test:assist`：设置页「辅助模型」小表每行写什么、什么颜色，加 `test:optrack`：发出的操作怎么等、回执后拉什么、气泡写什么；`test:snapmerge`：快照增量怎么并进上一份；`test:upgrade`：版本与更新的每句话、按钮能不能点、发件 upgrade 的校验；`test:progress`：专项「进度」三栏（负责人还没写简报时显示）的分栏、解锁、依赖图和服务端过滤；`test:brief`：专项「简报」标签的动作状态、话题的位置和影响、任务图、缺口和服务端过滤；`test:quota`：「账号与额度」页；`test:quotabrief`：看板右栏「额度」一块每个来源写几行、写什么；`test:theme`：深色、浅色切换图标放在哪、怎么叫，设置页不再有「外观」一块），自托管 3 组（`test:selfhost-logic` 12 条，纯逻辑；`test:selfhost-tls` 10 条，公网 HTTPS 的证书；`test:selfhost-api` 28 条，先构建自托管服务端，再起真服务把密钥、登录、会话、频控、登出、版本与更新和公网 HTTPS 走一遍）。

部署脚本的测试（不连飞书，用假的 lark-cli 和本机 git 仓库），在 covey 仓库根目录：

```bash
python3 -B -m unittest discover -s web/deploy/miaoda/tests
```

## 目录

| 目录 | 是什么 |
| --- | --- |
| `client/` | 前端：`client/src/cv/` 是工作台的页面和组件，`client/src/components/ui/` 是基础组件 |
| `server/` | 服务端：`server/modules/workbench/` 是网页的 `/api` 和给 Covey 的 `/openapi/sync/*` |
| `shared/` | 前后端共用的类型和数据约定（和 Covey 的 `core/web_sync.py` 对应） |
| `scripts/` | 单测，以及妙搭平台生成的构建、启动脚本（`build.sh`、`run.sh` 等） |
| `deploy/miaoda/` | 一键部署到妙搭：`deploy.py`、建表 `schema.sql`、测试 |
| `deploy/selfhost/` | 自托管：构建脚本 `build.sh` 和说明；服务端入口在 `server/selfhost/`，前端的登录页和垫片在 `client/src/selfhost/` |
| `licenses/` | 字体等第三方许可 |
| `.env.example` | 日志配置的默认值（见上面的「`.env` 和头像」） |

## 许可

代码随 covey 仓库用 MIT 许可（见仓库根目录的 LICENSE）。字体 Inter、JetBrains Mono 是 SIL Open Font License 1.1，头像的来源见 [licenses/README.md](licenses/README.md)。依赖里的 `@lark-apaas/*` 是飞书妙搭发布在公开 npm 上的包。

## 维护说明

`web/` 是网页工作台唯一的源码：改网页就改这里，提 issue 或 PR 都行。部署到妙搭时由 `deploy/miaoda/deploy.py` 把这里的代码推到你自己应用的仓库，不要反过来在应用仓库里改；自托管由 `deploy/selfhost/build.sh` 从这里构建。两种部署共用 `client/src/cv/` 和 `server/modules/workbench/`：改它们时两种构建都要过（`build:server`、`MIAODA_APP_TYPE=3 build:client`、`build:selfhost`）。

# 账号与额度

几个号、几个窗口、各自几点重置，Covey 用一种格式收：每个额度来源交一份**额度快照**（JSON）。Covey 只认这份快照，拿它画网页「账号与额度」、发提醒、写失败原因、定自动重试的时间。多一种来源，只要能写出这份 JSON 就行，页面不用改。

来源有四种：

| 来源 | 怎么拿 | 要配什么 |
| --- | --- | --- |
| 插件给的来源 | 扩展的 `quota_sources` 钩子给出地址（或命令），核心照常去读，比如公司扩展的「Claude 网关额度」 | 看扩展自己的说明 |
| 一条命令 | 命令的标准输出就是快照 | 数据根的 `quota-sources.json` |
| 一个网址 | GET 拿到快照，带来源自己的只读令牌 | 数据根的 `quota-sources.json` |
| agy 多账号 | Covey 按 agy 多账号的状态自己写，一个模型组一份 | 配了两个以上的号就有，见下文[agy 多账号](#agy-多账号) |

`COVEY_QUOTA=0`（或 `off`）全部关掉：不读、不问，失败卡照旧。扩展给的来源写在扩展自己的说明里。没有任何来源时 Bridge 不起额度线程。

## 快照格式（第 1 版）

字段只加不改；有变化要通知用过它的人。

```json
{
  "schema": 1,
  "title": "Claude · 我的 VPS",
  "collected_at": 1795683600,
  "ttl": 600,
  "pool": {"usable": 1, "total": 2, "next_available_at": 1795690800},
  "accounts": [
    {"alias": "A1", "state": "ok", "available_at": null,
     "windows": [{"kind": "5h", "label": "5 小时", "used": 0.42, "resets_at": 1795690800, "checked_at": 1795683000},
                 {"kind": "7d", "label": "周", "used": 0.80, "resets_at": 1796100000}]},
    {"alias": "A2", "state": "cooling", "available_at": 1795690800,
     "windows": [{"kind": "5h", "label": "5 小时", "used": 0.99, "resets_at": 1795690800}]}
  ]
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `schema` | 是 | 固定 1 |
| `title` | 否 | 页面上这一块的标题，最长 20 字 |
| `collected_at` | 是 | 这份快照的读数时刻，epoch 秒 |
| `ttl` | 是 | 来源自己认为多久内算新鲜（秒）；超过就灰字标“N 分钟前的读数”，超过两倍不显示数字。Covey 按 30 秒到 7 天截取 |
| `pool` | 否 | 这一池几个号能用、共几个、都不能用时最早几点有号能用。不给就由 Covey 按 `accounts` 数 |
| `accounts[].alias` | 是 | 中性别名，最长 20 字。不要用邮箱、真名、订阅档位 |
| `accounts[].state` | 是 | `ok` 能用、`spent` 用完、`cooling` 冷却中、`paused` 暂停、`login_required` 要重新登录、`expired` 到期、`unknown` 读不到 |
| `accounts[].available_at` | 否 | 不能用时最早几点能用（epoch 秒）；能用时 null。**能不能用由来源判断**（网关有保留线、保护线、按 Key 限号），Covey 不从百分比推 |
| `accounts[].active` | 否 | 只对“一次只用一个号”的来源（agy）：当前在用的那个 |
| `accounts[].expires_at` | 否 | 订阅或一次性号的到期时刻 |
| `accounts[].login` | 否 | 这个号登录用的邮箱（agy 从自己的 `cli.log` 读，页面上整个写出来）。像邮箱、80 字以内才留，其余丢掉；**这是唯一不脱敏的文字**，别的来源别往里写不该给主人看的东西 |
| `accounts[].windows[].kind` | 是 | `5h`、`7d`、`day`、`other`，用来排序；别的写法当 `other` |
| `accounts[].windows[].label` | 否 | 显示名，最长 12 字，如“Fable 周” |
| `accounts[].windows[].used` | 是 | 已用比例 0 到 1；**读不到写 null，不要写 0** |
| `accounts[].windows[].resets_at` | 否 | 这个窗口几点重置，epoch 秒；不知道就不写 |
| `accounts[].windows[].checked_at` | 否 | 这个窗口的读数时刻；不写就按 `collected_at` |
| `accounts[].windows[].ceiling` | 否 | 停派线（0 到 1），页面在条上画一道 |
| `link` | 否 | 一个 https 链接，页面上给池主一个“去运维台”的入口 |

不许出现：邮箱（`login` 除外）、IP、地址、Key 或令牌、费用、请求数、订阅档位。单份不超过 64 KB，号不超过 50 个，每个号的窗口不超过 8 个。Covey 收下时只留上表的字段（`core/quota.py` 的 `parse`），文字里像邮箱、网址、本机路径、IP、密钥的部分换成“…”，别名只剩这些时整份不收。

## 插件来源

扩展可以在 manifest 的 `HOOKS` 里挂 `quota_sources`：模块的 `sources(environ)` 返回 `covey.core.quota_sources.Source` 的列表，核心和自己的来源一样去读、存、画。

- 种类：`gateway`（GET 一个地址，带 `key` 放进 `Authorization: Bearer`；从没读到过快照时回 404 当没有额度接口，不显示、6 小时后再问；读到过以后再回 404 只算一次没读到：网关自己的读数过期时也回 404，这时保留上一份、页面写原因，按平常的间隔再读）、`url`、`command`。
- 地址只认 https，或者本机回环的 http（隧道的本地一端）；不跟随跳转，回答最多读 64 KB。
- `claude=True` 表示这是 Claude Code 自己登录背后的号池：Claude 一轮因额度或限流失败时，失败卡按它说清几点恢复（见下文[失败说明](#失败说明)）。
- 一个扩展的钩子出错只影响它自己的来源，`bridge.log` 里写包名。
- 来源这一次没给出来（钩子出错、它读的配置文件一时读不了），已有的快照照留，页面照画；一小时没读过才删（`quota_sources.forget_gone`）。
- 一个来源开始读不到时 `bridge.log` 记一行 `quota source <来源>: read failed: <原因>`，再读到时记 `read again`，各一次（`app/quota_watch.py`）。

## `quota-sources.json`

放在数据根，Bridge 每分钟看一次（不用重启）。它写的是要执行的命令，所以只在**属主是你、别人不能写**（文件和它所在的目录都没有组、其他人的写权限）时才用；不满足时 `bridge.log` 记一行原因。

```json
{"sources": [
  {"id": "vps", "kind": "command", "argv": ["/absolute/bin/quota-snapshot"], "interval": 300, "timeout": 30, "title": "Claude · 我的 VPS"},
  {"id": "team", "kind": "url", "url": "https://quota.example.com/me", "token_file": "/absolute/quota-token", "interval": 600}
]}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 小写字母、数字、点、横线，不以 `agy`、`gateway` 开头，不重复 |
| `kind` | `command` 或 `url` |
| `argv` | 命令和参数，第一项是绝对路径；不经过 shell |
| `url` | https 地址（或本机回环的 http）；只带 `token_file` 里的令牌（`Authorization: Bearer`） |
| `interval` | 多久读一次，秒，60 到 86400，默认 300 |
| `timeout` | 多久没结束就停，秒，1 到 120，默认 30；命令超时停整个进程组 |
| `title` | 页面上的标题（快照里写了 `title` 的以快照为准） |

命令往标准输出打一份快照、退出码 0 就算读到；非 0、超时、输出不是快照，都算这次没读到，Covey 保留上一份。一个最小的例子（把自己 VPS 的读数翻成快照）：

```python
#!/usr/bin/env python3
"""quota-snapshot：把 VPS 上的额度读数翻成 Covey 的额度快照，打印到标准输出。"""
import json, subprocess, sys, time

raw = json.loads(subprocess.run(["/absolute/bin/my-vps-quota", "--json"], capture_output=True, text=True,
                                timeout=20, check=True).stdout)
accounts = [{"alias": f"A{n}", "state": "ok" if row["usable"] else "cooling",
             "available_at": None if row["usable"] else row.get("ready_at"),
             "windows": [{"kind": "5h", "label": "5 小时", "used": row.get("five_hour"), "resets_at": row.get("five_hour_reset")},
                         {"kind": "7d", "label": "周", "used": row.get("week"), "resets_at": row.get("week_reset")}]}
            for n, row in enumerate(raw["accounts"], 1)]
json.dump({"schema": 1, "title": "Claude · 我的 VPS", "collected_at": time.time(), "ttl": 600, "accounts": accounts}, sys.stdout)
```

## 失败说明

一轮因为额度或限流失败时，失败卡按快照说清楚：

- **Claude Code 登录背后的号池一个能用的都没有**（插件标了 `claude` 的来源，快照超过 30 秒先真查一次）：
  - 有会自己恢复的号：原因改判为“额度用完”，写“网关里的号额度都用完了，最早 14:20 恢复”，不管网关回的是 429 还是 503。
  - 都要重新登录、已到期或暂停：写“网关里没有能用的号，要网关的主人处理”，不重试。网关这时回 503，Claude Code 自己会先重试 3 分钟，Covey 不再按“临时故障”加重试。
- **网关的 429 说额度用完**（“quota usage limit has been reached”“wait for quota to reset”这类说法，`core/limit_reset.py` 的 `quota_spent`）：没有快照也归“额度用完”；文字里有“(in 9h50m)”就按它算恢复时刻。
- **agy 每个号的这一组都用完了**：写“2 个号的 Gemini 额度都用完了或要重新登录，最早 17:10 恢复”。
- 额度用完、知道几点恢复、而且在 6 小时内：恢复后 1 分钟自动接着做（一条链最多 3 次，`app/auto_retry.py` 的 `QUOTA`）。超过 6 小时（多半是周额度）或不知道几点：不自动等，卡上写最早几点恢复、可以换个模型接着做；知道几点恢复的，到点飞书提醒一句（见[页面和提醒](#页面和提醒)）。`COVEY_AUTO_RETRY_LONG=0` 时额度用完照旧不重试。

## agy 多账号

本机有几个 Google 账号时给 Antigravity（`agy`）用：一轮里当前账号这个模型组（Gemini 一组，Claude/GPT 一组）的 5 小时或周额度用完了，或者它的登录失效了，就换下一个账号，在同一个对话里接着做，执行过程里多一条“主号的 Gemini 5 小时额度用完了（14:20 恢复），换副号接着做”。agy 一台机器只有一个登录、没有切账号的参数，只按 `HOME` 找登录，所以一个账号一个目录；对话是本机文件，换了账号也接得上（agy 1.5.14 实测）。默认关：配了第二个号才开。

**风控口径**：一直是官方 `agy` 自己登录、自己发请求，不导出令牌、不走代理。同一台机器、同一个出口上几个账号接力用，谷歌可能把它们关联起来，所以 Covey 只在确认用完时换、不来回切、不轮流用，也不定时去查备用号；不要同时用几个号叠加额度跑同一批活。多个 Google 账号接力用在谷歌的使用条款上是灰区，开不开、开几个号由你自己判断。

**打开**

1. 把终端命令装到 PATH（一次）：`python3 /absolute/covey/bin/agy_as.py --install`，写到 `~/.local/bin/agy-as`（后面可以跟别的目录）。装的是一份自带全部代码的文件，升级、删检出都不会让它失效。
2. 登录副号：`agy-as b`。第一次运行时建 `~/.agy-accounts/b`（`AGY_ACCOUNTS_DIR` 可换目录），在 agy 里选 Google OAuth，用副号登录：没有浏览器时打开它给的链接，把授权码贴回终端。之后 `agy-as b <agy 的参数>` 就是用副号跑 agy，`agy-as --list` 列出账号和是否已登录；直接敲 `agy` 仍是本机原来的登录。新账号第一次用时谷歌可能要求“验证是您本人”，按 `/usage` 报错里的链接在登录了那个账号的浏览器里做完。
3. 在数据根写 `agy-accounts.json`（或用 `COVEY_AGY_ACCOUNTS` 指别的文件），下一轮生效，不用重启：

```json
{"accounts": [{"name": "主号"}, {"name": "副号", "home": "/absolute/.agy-accounts/b"}]}
```

不写 `home` 的是本机原来的登录（最多一个），其余写账号目录；顺序就是换号的顺序；一行写 `"enabled": false` 关掉那个号，整个文件写 `"enabled": false` 全关、文件留着。少于两个号时不起作用。

**以前用 covey-internal 的 agy 多账号的**：配置文件、账号目录和状态目录原地沿用，不用改。只有终端命令要重装一次：原来的 `agy-as` 是指向扩展检出里文件的软链，扩展删掉这个文件后它就悬空了，跑一次上面第 1 步（它会替换掉那条软链）。设置卡「诊断」看到“agy-as 要重装”就是这件事。核心自带的换号和某个扩展的 `agy_turn` 钩子不能同时开，升级检查会拦下并说是哪个扩展。

**怎么换**

- 每个模型组各有一个“在用的号”。一轮从它这一组的在用号开始；这个号在这一组等额度恢复、要重新登录或被暂停，就按顺序找第一个能用的，它成为这一组的在用号。在用号一直用到它失败才换，不来回切，主号恢复了也不切回去。
- 一轮失败时（核心把额度用完、限流、没登录三种交给它），先用 `agy -p /usage --output-format json`（不耗 token）看这个号：这个组的 5 小时或周额度剩 1% 以下才算用完；agy 自己的报错说额度用完（`exhausted your quota`、`exhausted your capacity`……）而 `/usage` 剩 10% 以下或者查不到，也算（`/usage` 可能慢一两次请求）。还有额度的 429 不换号，交给核心的自动重试。算用完的记到那个额度的恢复时间，再按顺序找下一个这一组还有额度的号，在它那里接着同一个对话。
- 换号在这一组的锁里做：几个话题同时撞上用完时，第一个选好，其余的看到在用号已经变了，直接用新号，不各自去查。已经在跑的轮次不受影响。
- agy 自己报登录失效的号标“要重新登录”，一直跳过，直到它重新登录（登录文件变了）或者 `/usage` 又查得到；不是“跳过 30 分钟再试”。
- `/usage` 查不到（网络抖一下、超时）不当成没登录：记“没查到”，换号时照样可以换过去，agy 自己会说行不行。
- 一个号都不剩：这一轮按“额度用完”失败，写最早几点恢复，见上文[失败说明](#失败说明)。
- agy 已经收下这一轮的话时，新账号收到一句“接着完成上面的要求”，没收下就把原话再发一遍（`backends/agy/bridge.py`）。每轮最多换 3 次。
- 页面上的「暂停」（不参与轮换）和「只用这个号」（只用的号用完了就等它恢复，不换走）写在状态里，重启不丢（`backends/agy/accounts.py` 的 `set_choice`）。

**账号目录**

- 每一轮给 agy 拼一个 `HOME`（`<状态目录>/tmp/agy-account-*`，跑完删掉）：真实主目录的每一项都链回原处，只有 `.gemini/antigravity-cli`（登录、缓存、日志、agy 自己的设置）和 `.config/antigravity` 链到账号目录。所以 `.ssh`、git、lark-cli、主人的 GEMINI.md、MCP 服务、hooks、skills 照常；令牌刷新写回账号目录。管家那一轮的私有主目录叠在它上面。
- 账号目录的 `antigravity-cli` 里，`conversations`、`brain`、`implicit`、`presence`、`persistent_terminals` 是指向本机原登录那份的链接，几个账号共用对话。账号以前自己存过的这几个目录改名成 `<名字>.own-<时间>` 留在旁边，不删。
- 设备上的轮次、小模型判断（`ask_once`）和启动卡的模型目录（`agy models`）不经过它，用本机原来的登录。

**邮箱和模型池**：页面上 agy 每个号写它登录的 Google 邮箱：取这个号自己的 `antigravity-cli/cli.log`（本机原来的登录在真实 HOME 的 `~/.gemini/antigravity-cli/`，其余号在它 `home` 目录下同路径）最后一行 `OAuth: authenticated successfully as <邮箱>`，写进快照的 `accounts[].login`；读不到时用状态里记着的上一个，都没有才显示 `agy-accounts.json` 里的名字。**只读这份日志，绝不读 `antigravity-oauth-token` 这类令牌文件。** 页面上一个号一行，只画它近 7 天真跑过的模型池（看 `turns.jsonl` 的 group，写在两份 agy 快照各自的 `recent` 里）：没跑过的备用号取任何号跑过的池，都没跑过就是 Gemini；两个池都用过的号才在同一格上下两行。

**页面上的读数**：agy 一个模型组一份快照（来源 `agy.gemini`、`agy.3p`），按状态现做：谁在用、谁用完到几点、谁要重新登录或暂停，加上每个号最近一次 `/usage` 的读数（换号时查的，或者你在页面上点「刷新」时查的；页面一期没有「暂停」「只用这个号」「查全部账号」的入口，`set_choice` 留在后端）。备用号从不定时去查，所以它的读数常是几小时前的，页面照实标出。

## 页面和提醒

网页「账号与额度」（样子和数据见 [web.md](web.md#账号与额度)）读上面这些快照；右上角「刷新」让 Bridge 立刻重读所有来源、agy 查每个号的 `/usage`（5 分钟冷却）。飞书只在两件事变成真的那一刻说一次：某个来源一个能用的号都没有了（带最早恢复时刻）、有号要重新登录；同一件事不重复，夜里按 `COVEY_QUIET_HOURS` 攒到早上（`app/quota_notice.py`）。

第三件：一轮因额度用完失败、知道几点恢复但超过 6 小时（`auto_retry.MAX_RESET_WAIT`，所以不排自动重试）时，Bridge 登记恢复时刻；到点后在通知的聊天里说一句“Claude 网关额度已恢复（17:10）：T12 某某话题 停在额度用完，可以接着做”，带话题链接；来源名是 Claude 登录背后号池的标题、agy（带模型组）或 Harness 的名字，几个来源的话题按来源分开写。同一话题、同一恢复时刻只说一次，几个话题同时到点合成一条；夜里攒到早上；期间话题有了新的一轮、或点过“检查并继续”的不说（`app/quota_return.py`，挂在 Bridge 的发件循环上，不另起线程）。

## 记录

| 路径 | 内容 |
| --- | --- |
| `<状态目录>/quota/<来源>.json` | 每个来源最近一份快照、最近一次读的时刻、成功的时刻、没读到的原因、网关有没有这个接口 |
| `<状态目录>/quota/returns.json` | 额度用完且 6 小时以上才恢复的话题：话题、失败的任务、恢复时刻，状态 waiting（等到点）、said（已说）、dropped（话题已接着做，不说了）；说过或作废的留 7 天 |
| `<状态目录>/quota/refresh.json`、`notices.json` | 页面上最近一次「刷新」的开始和结束时刻（5 分钟冷却的依据）；说过的提醒（同一件事不重复说） |
| `<状态目录>/agy-accounts/state.json` | 每个组的在用号、各号登录用的邮箱、各号每个组到几点恢复、要重新登录的号、没查到的号、模型属于哪个组、最近的 `/usage` 读数、暂停和只用、最近一次换号；旧文件的一个“当前账号”当作每个组的 |
| `<状态目录>/agy-accounts/accounts.log` | 每轮用的号、每次 `/usage` 和换号；不记令牌、问题和回答 |
| `<状态目录>/agy-accounts/turns.jsonl` | 每轮跑在哪个号上（任务的 run id、组、模型），「用量」按号分用 |

都只在本机，不推到别处；网页工作台只拿快照里白名单的字段。

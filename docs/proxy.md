# 代答

代答替主人回复白名单里的人和群：Bridge 以主人身份读这些会话，每个联系人在代答群里有一个话题，模型在话题里写回复，Bridge 以主人身份发出去。用户看到的行为见[使用指南 · 代答](usage.md#代答)，部署的配置见[配置 · 代答](configuration.md#代答)。

## 前置条件与怎么开

**只装核心用不了代答。** 以主人身份发消息的“发送器”不在核心里，由扩展的 `proxy_sender` 钩子提供（[发送器](#发送器)）；没有启用带发送器的扩展时，菜单里没有「代答」、没有代答的后台线程，话题也不能[代发](#代发)。

1. **启用带发送器的扩展**：照那个扩展的 README 装好并启用（[扩展的装和卸](operations.md#扩展的装和卸)），发送器自己要的登录、工具也照它的说明做。
2. **给主人本人建一个 lark-cli profile**，用主人自己的账号登录并授权读消息、搜联系人的权限（清单见[配置 · 代答](configuration.md#代答)），再把它的名字写进服务清单：按[改开关三步](configuration.md#改开关三步)设 `COVEY_PROXY_PROFILE=<profile 名>`。
3. **在飞书里开启**：菜单「代答」→ 代答卡上按提示“去登录”（发送器的扫码登录）→「开启代答」。第一次开启时机器人新建一个只有你和它的代答群，群名默认「代答」，要换名字在服务清单里设 `COVEY_PROXY_GROUP_NAME`（只影响之后新建的群；已经建好的群按群 ID 找，名字照旧）。
4. **加白名单**：在代答卡的“添加”里写人名、邮箱、群名或 ID，点“查找”再添加。之后白名单里的人发来的消息，各自在代答群里有一个话题。
5. **确认**：代答卡状态行写“代答开着”；`bridge.log` 有 `proxy took in`。不回、不发时看[排障 · 代答](troubleshooting.md#代答没有回复或没有发出)。

下文写实现，给改代答代码的人看；和记录、锁、结果授权这些全局规则的接点见[架构与开发说明的代答](architecture.md#代答)。

## 组成

`app/proxy.py` 的 `Proxy`（`UnifiedBridgeApp.proxy`）是组合根，自己只留两把锁、唤醒和批次状态机，其余交给各部分：

| 部分 | 属性 | 文件 | 做什么 |
| --- | --- | --- | --- |
| 批次推进 | `Proxy` 本身 | `app/proxy.py` | 后台线程 `proxy`（`run` → `tick` → `advance`）：并消息、提交、判断、发前闸门、发送收尾 |
| 收消息 | `intake` | `app/proxy_intake.py` | 后台线程 `proxy-in`（`ProxyIntake.listen`）以主人身份读会话，新消息进 `inbox` |
| 联系人话题 | `contact_topics` | `app/proxy_topics.py` | 开、重新找到、路由联系人的话题，跟着设置换模型（`sync_topics`） |
| 设置与查找 | `commands` | `app/proxy_commands.py` | 开启、关闭、换模型、查找联系人、读群成员、加删白名单 |
| 发送 | `web` | `app/proxy_send.py` | 经发送器以主人身份发送、读回、`owner_replied` |
| 登录 | `web_login` | 发送器所在扩展的登录模块 | 发送工具的扫码登录、定时查登录、失效提醒、恢复后重发 |
| 设置卡 | `panel` | `app/proxy_panel.py`、`cards/proxy_panel.py` | 设置卡的「代答」页 |
| 重新发送卡 | `resend` | `app/proxy_resend.py`、`cards/proxy_resend.py` | 没发出的回复，主人点了再发 |
| 记录 | | `core/proxy.py` | `<state>/proxy/` 的读写、状态表、提示词、标记；只用标准库，不调飞书 |

只有 Bridge 写代答记录，任务进程不碰。各部分之间的导入方向由 `tests/test_structure.py` 检查：`proxy_intake`、`proxy_login` 只能导入 `proxy_send`，`proxy_topics` 不导入别的部分。

## 两把锁

- **`Proxy.lock`**（`core/proxy.py` 的 `LOCK`）：联系人和批次的读改写。锁内只读写记录，不发网络请求（规则 1）。设置卡、重新发送卡的点击和后台线程都经它改记录。
- **`Proxy.web_lock`**：同一时刻只跑一次发送工具（发送或登录）。拿不到时这一轮不发，下一轮再试。`check_login`、维护删浏览器缓存也只在拿得到时做。

顺序固定：`Proxy.send` 先不等待地拿 `web_lock`，再短暂进 `Proxy.lock` 复查闸门、把批次转 `sending`，出锁后仍持 `web_lock` 发送。`ProxySend`、`ProxyLogin` 不碰 `Proxy.lock`（`tests/test_structure.py` 检查）。

## 收消息

IM 事件只有机器人身份，看不到主人的会话，所以 `ProxyIntake.listen` 以主人身份（`settings.user_profile`，来自 `COVEY_PROXY_PROFILE`）读：

- **逐个读**：每 `ROUND_SECONDS`（1 秒）一轮，对知道 chat_id 的会话调 `im +chat-messages-list`（同轮并行）。距上次满 `LIST_SECONDS`（3 秒）到期；活跃会话（`ACTIVE_SECONDS` 内收过消息或有批次在推进，`mark_active`）满 `ACTIVE_LIST_SECONDS`（2 秒）。一轮至多 `LISTS`（4）个，等得最久的先读。这样每分钟至多约 240 次，给主人这个用户应用的其他用途留出余量。
- **搜索兜底**：每 `SEARCH_SECONDS`（60 秒）另起后台线程 `proxy-search`（`ProxyIntake.searching`，上一次没完就不起），调 `im +messages-search`：白名单群一次 `--is-at-me`，私聊一次 `--chat-type p2p --sender-type user`，各读至多 `SEARCH_PAGES` 页。它补上还没有 chat_id 的私聊和不在主列表里的话题回复。超过页数时日志 `proxy search truncated`。
- **过滤**（`new_messages`）：只收对方发的；群里只收 @主人的；私聊联系人只收私聊里的；`seen` 去重，`since` 之前的不收。open_id 按应用不同：联系人和 `owner_user_id` 是用户应用里的 ID，群和话题是机器人的。
- **交接**：新消息放进内存的 `inbox`，唤醒 `Proxy.run`。批次只由 `run` 的后台线程写（`tick` 开头 `drain_inbox`，`add_messages` 在锁内再去重），所以收消息不会和批次推进抢记录，发送也拖不住收消息。代答关闭时不读。
- **失败**（`guarded`）：每个来源单独退避，`BACKOFF_FIRST`（10 秒）起加倍到 `BACKOFF_LIMIT`（600 秒）；只在第一次失败记 `proxy take-in failed source=…`，恢复时记 `proxy take-in reads again`。Bridge 停止时被结束的读取不算失败。平时回看 `LOOKBACK`（15 分钟）；一个来源连续失败更久后，下次成功从上次成功的时间读起，至多回看 `CATCH_UP`（2 小时）。

`run` 每 `POLL_SECONDS`（30 秒）一轮；收到新消息、批次到点（`Proxy.soon`）或代答群里的任务结束（`Runtime.job_ended` → `Proxy.job_ended`）时立刻一轮。

## 批次

一个批次是一次回复要答的消息，记录在 `<state>/proxy/batches/<id>.json`，状态只按 `core/proxy.py` 的 `BATCH_TRANSITIONS` 前进：

| 状态 | 下一步 | 含义 |
| --- | --- | --- |
| `received` | `submitted`、`superseded`、`skipped` | 收消息中，等会话安静 |
| `submitted` | `sending`、`held`、`superseded`、`skipped` | 任务在跑，或跑完还没判 |
| `held` | `sending`、`superseded`、`skipped` | 先不发，等主人在话题里的下一轮或重试 |
| `sending` | `held`、`sent`、`failed`、`uncertain` | 正在发 |
| `sent`、`uncertain`、`failed`、`skipped`、`superseded` | 终态 | `uncertain` 是可能已发出，从不重发；`failed` 只有旧版本写过 |

`history` 每次转换记 `[状态, 时间, 原因]`（`core/proxy.py` 的 `history`），`reason` 只是最新的一个。

- **并消息**：一个联系人同时只有一个在推进的批次。新消息并进 `received` 的（没有就开一个），替换 `held` 的。`received` 等会话安静 `SETTLE_SECONDS`（10 秒）才提交，从开批次算至多 `SETTLE_LIMIT`（45 秒）。
- **顶掉**：新消息到时有 `submitted` 的批次，`add_messages` 在锁内先写新的 `received` 批次（旧消息在前，`restarts` 加一，`stop_job` 记旧批次），再把旧批次转 `superseded`（`OVERTAKEN`）；锁外 `stop_overtaken` 照任务卡“停止”停它的任务（`stop_job`：任务进程活着时转给它停，否则 `TaskControls.cancel_reply_job_from_card`），然后清掉 `stop_job`。Bridge 中途退出时下一轮 `tick` 补做。旧批次的任务正在跑、提交不到 `OVERTAKE_STOP_AFTER`（90 秒）时不停（`Proxy.runs_on`）：旧批次照样转 `superseded`（`OVERTAKEN_RUNS_ON`），回复不发，不记 `stop_job`，话题里说一句“这一轮跑完不发”；新批次的任务排在它后面，接着它的原生会话跑（原生会话只有正常结束的一轮才能续接，停下的一轮会丢掉会话，新一轮得从头读一遍）。还在排队的、跑得更久的照旧停。`sending` 的从不打断；话题里已有主人更新的一轮时不顶；连着顶掉 `MAX_RESTARTS`（2）轮后这一轮照旧跑完。
- **提交**（`submit`）：批次以镜像卡为消息，经 `Submission.enqueue_chat_job` 提交到联系人话题，提示词由 `core/proxy.py` 的 `batch_prompt` 写：对方的消息；这个会话最近 `RECENT_MESSAGES`（20）条聊天（`ProxySend.recent_chat` 以主人身份读，含主人亲手发的，每条至多 `RECENT_CHARS` 字；读不到时不附，提示词改让模型自己去读）；怎么回（主人在对方聊的事上是行家，接住话头、给具体内容，不用外行式的反问代替回答）；主人在做的事 `<state>/proxy/owner-context.md`（`read_context`，主人确认能对外说的项目和术语）；最后是主人风格说明 `<state>/proxy/style.md`（`read_style`）。联系人笔记的 `README.md` 顶部是摘要，每轮的过程记录追加到同目录 `log.md`。镜像卡 id 先存进批次，重启后按它找已有任务，不重复提交。提交前主人已亲自回复的、提交没成的转 `skipped`。
- **判断**（`decide`）：只用 `load_authorized_reply` 取已授权的保存回复（规则 5）。
  - 以 `NO_REPLY_MARK`（`[无需回复]`）开头：`skipped`，话题里一行“· 不用回”。
  - 以 `SKIP_MARK`（`[不回复]`）开头、没有回复或任务没完成：`held`，等主人在话题里的下一轮；回复带 `SUGGESTION` 行时存下建议，出重新发送卡的“按建议回复”。
  - 话题里有比这条任务更新的一轮（主人的）时，本该发的回复不用，批次转 `held`（`LATER_TURN`），`passed_over` 记被跳过的任务；`check_held` 对每一轮判断，所以发的总是最新一轮的回复。
  - 批次自己的任务结束时联系人已有更新的批次：直接 `superseded`。
- **移出和关闭**：联系人移出白名单后，它没发完的批次下一轮转 `skipped`（`REMOVED`）；早于联系人 `added_at` 的批次按移除处理。关闭代答后不再收新消息，`received` 转 `skipped`，其余照常推进但不发。一个批次推进出错只记 `proxy batch=… failed`，不挡别的批次。
- **日志**：`proxy batch=… job=#N runs on: …`、`proxy overtaken batch=…`、`proxy stop job=#N batch=…`、`proxy passed over batch=…`。

## 话题

`ProxyTopics`。联系人的话题像分叉一样在 `topic.json` 绑定 Harness、模型、工作区并 `workspace_locked`，另记 `proxy`（contact、chat_id、root_id），所以不出启动卡。

- **开和找**（`ensure_topic`）：移出又加回的联系人沿用原来的话题和会话（`earlier_root` 按 `topic.json` 找）；从没有话题的才发起始卡，幂等键带这次加入的 `added_at`（`feishu/transport.py` 永久记着用过的键）。
- **路由**（`topic_context`）：批次的任务按话题自己绑定的 Harness 找 app，和话题里的普通消息一样。设置卡上换 Harness 只影响新话题。
- **换模型**（`sync_topics`，每轮 `tick` 跑）：设置卡改了模型时记 `settings.model_changed_at`；联系人的 `model_synced_at` 早于它、话题没有 `submitted`/`sending` 批次时，用 `app/binding.py` 的 `switch_topic` 换绑定，话题里提示一句。主人在话题里单独换的不会被改回，直到卡上再改。
- **不等主人**：`app/runs.py` 的 `_leave_checkpoint_question` 清掉代答话题检查点里的提问；Claude、Codex 的原生提问由 `app/native_questions.py` 的 `refused_in_proxy_topic` 以 `NO_QUESTION` 拒绝，模型接着写回复。`HarnessSpec.refuses_questions` 为 False 的 Harness（扩展的 Harness）的提问不拒（会取消整个运行）。
- **主人的一轮**：主人在代答话题里说话、而联系人有 `submitted` 或 `held` 批次时，`app/runs.py` 的 `_turn_message` 附上 `owner_turn_note`：这一轮的回复会发给对方，不要发就以 `SKIP_MARK` 开头。
- **规则**：代答话题的轮次用 `app/prompts.py` 的 `proxy_constraints` 代替普通话题的规则（不用 Markdown 排版、不写检查点和话题要点，只读查询，不发飞书消息），不附检查点说明（`Runs._checkpoint_note`），续接的会话也不再追加普通话题的提醒；新会话的 “Recent conversation” 里，之前每批的提示词只留消息（`core/proxy.py` 的 `brief_history`），规则只在这一批里出现一次。
- 代答话题不带主人的原则；产物不列代答联系人的笔记目录：扩展用 `contact_notes` 设置（一个正则）指出它在哪，核心默认没有（`app/artifacts.py` 的 `drive_paths`）。

## 发前闸门

`Proxy.send` 发之前按最新的设置和联系人记录判断能不能发（`stopped`）：

| 情况 | 结果 |
| --- | --- |
| 代答关闭、联系人暂停 | `held`，话题里说明（`stop`），主人之后的一轮才发 |
| 联系人移出 | `skipped` |
| 批次自己任务的回复距对方最新消息超过 `STALE_AFTER`（2 小时） | `held`；主人的一轮和重新发送不受这条限制 |
| 主人已亲自回复（`ProxySend.owner_replied`） | `skipped`，话题里说一句 |
| 一小时内已发满 `HOURLY_LIMIT`（10）条 | 批次自己的回复 `held`；主人的一轮、重发返回 `LATER`，话题里说一次几点后自动发出（`next_send_at`） |
| 拿不到 `web_lock`（发送工具在登录） | 返回 `LATER`，下一轮再发 |

`stopped` 在 `Proxy.lock` 内、转 `sending` 前再判断一次。卡上的暂停、删除、关闭也在这把锁内写，所以它们要么让这次不发，要么见到的已是 `sending`。

回复里的 at 标签一律变成纯文字（`plain_mentions`）：`<at user_id="all">所有人</at>` 变成 `@所有人`，`<at user_id="ou_…">张三</at>` 变成 `@张三`。保存的 `reply`、幂等键、读回比对和重新发送卡都用换过的文字。

## 发送

转 `sending` 后在锁外经[发送工具](#发送器)发（`ProxySend.web_send`），收尾在 `finish_send`：

- **确定没发出**（`submitted=false`）：转 `held`（`send_failed`）再试；`SEND_ATTEMPTS`（3）次后不再自动试，在话题里出[重新发送卡](#重新发送卡)。换了新回复（主人的一轮）时次数清零。
- **已发出**：`sent`，话题里一行“✅ 已代你回复（上一条，HH:MM 发出）”。`sent_message_id` 用回执的 `om_`；为空时之后的轮询再读几次（`ProxySend.read_backs`，共 `READ_BACKS` 次），找到就补上（`Proxy.update`）。
- **可能已发出**（`unknown_result`、超时、Bridge 在发送中停了）：只用用户身份读回，找到是 `sent`，否则 `uncertain`，从不再发（规则 1、6）。启动时见到 `sending` 只读回。
- 日志 `proxy send batch=… ok= type= submitted= message_id=`。

## 发送器

代答以主人身份发消息要有一个发送器：扩展钩子 `proxy_sender`（`core/extensions.py`）提供的模块，有 `Sender`（`web_send`、`run_tool`、`problems`）、`ProxyLogin`（登录检查、失效提醒、恢复后重发、缓存清理）、`tool_dir()` 和 `send_problem(settings)`。核心的 `app/proxy_send.py` 只管读主人的聊天（读回、`owner_replied`）和把发送交给发送器；没有发送器（没有启用提供它的扩展）时不起代答的两个后台线程、机器人菜单没有「代答」、设置卡没有「代答」标签。

公司部署的发送器，安装、运行、环境、幂等键和登录的说明在扩展自己的 README。

## 设置卡

代答设置是机器人菜单单独的一项「代答」（事件键 `proxy_settings`，`ProxyPanel.open`，再点收起 `fold`；`core/app_manifest.py` 只在启用的扩展有 `proxy_sender` hook 时要求这一项）。设置卡里记录还停在「代答」标签的旧卡，仍由 `app/settings_hub.py` 经 `tab_card` 画。画卡在 `cards/proxy_panel.py`：状态行、开关与登录、白名单、添加，最后是模型行；网页登录有效与否取自发送器登录状态 `web_login.login_seen`（开着代答时才轮询，`ProxyPanel.card` 算成 `login_ok`），没确认过按失效画“去登录”。

- **记录**：独立发的卡记在 `<state>/proxy/panels/<token>.json`，从设置卡打开时记在设置卡的记录里（`app/menu_panel.py`）；`latest.json` 指向最新的一张，只有它能操作，点旧卡整卡换成过期提示。
- **点击**：控件的值带 `bridge_proxy_*` kind（都在 `ACTIONS`，不是 `bot_only`）、token 和画出时的 revision。`app/card_routing.py` 按 `PANELS` 表交给 `ProxyPanel`（`claim`、`click`），不经话题路由，不转发任务进程。点击在本卡的锁内判断，记录改动经 `Proxy` 的方法，然后整卡重画（`replace_form_card`，`reserve` 取 sequence），从不 PUT 表单里的单个组件。非主人写在卡上（`OWNER_ONLY`）。
- **旧 revision 的点击**：每个动作自己判断还适不适用（`act`/`slow` 抛 `Refused`）：查找、刷新、翻页、选模型、登录照做；添加要求查找结果还在；暂停、恢复、删除要求条目还在、状态没变；确认只能点一次。不适用时卡上写 `STALE`。
- **慢操作**（开启、查找、读群成员、确认添加）：点击里只记视图和 `working` 里的忙碌提示，立即返回；`after_ack` 的后续里先画忙碌卡，同时做事，再画结果。两次写都从 `reserve` 取 sequence。
- **查找**（`ProxyCommands.lookup`）：输入框按 `core/proxy.py` 的 `parse_names` 拆成至多 `NAMES`（20）个名字，查人（`contact +search-user`）和查群（`im +chat-search`）并行（`parallel`，至多 `LOOKUPS` 个）。每个名字是唯一匹配、多个候选、没有或出错；“全部添加”只加唯一匹配且不在白名单的。没有文字命令。
- **群成员**（`ProxyCommands.group_members`）：以主人身份读成员（至多 `MEMBER_PAGES` 页），去掉主人和已在白名单的，作为确认页；超过 `MEMBER_LIMIT`（500）人不给确认。确认后每人一条私聊联系人，带 `source`；同一来源在卡上折成一行，批量操作经 `set_source_paused`、`remove_source`。
- **选模型**：Harness、供应商、模型、思考预算四行，和启动卡、设置卡另两页一样是设置行（`cards/launch_config.py` 的 `harness_setting`、`tier_rows`，不给空选项；不用下拉框）：行在客户端展开，点一项整卡重画。点 Harness 或供应商换到它的第一个模型、用那个模型的默认预算，模型已在其中就不变；点和现在一样的一项只重画，写“没有变”。点击经 `ProxyPanel.pick`，保存经 `set_model`（改了才记 `model_changed_at`，`sync_topics` 据此换已有话题）。旧卡的两个下拉框（`bridge_proxy_model`、`bridge_proxy_budget`，选项在事件的 `option` 里）照旧生效（`choose`）。
- **版面**：白名单按节点预算分页（`PAGE_NODES`），超过 `FOLD_ROWS` 行收进折叠面板；候选先放唯一匹配，每个多候选的名字给 `FIRST_CHOICES` 个；放不下时模型行少列几个模型（`MODEL_ROWS`，当前的总在）。
- **回调响应里带卡**：做不到。卡片回调由 lark-cli 的事件总线进程持有长连接并自己回响应，Bridge 只读它的标准输出，没有回写通道。

## 重新发送卡

`ProxyResend`（`app/proxy_resend.py`，卡 `cards/proxy_resend.py`，kind `bridge_proxy_resend`）。

- **什么时候发**：`finish_send` 在 `SEND_ATTEMPTS` 次都确定没发出后，在批次的镜像消息下发这张卡：原因（`core/proxy.py` 的 `unsent` 写成一句话）、要发的原文、“重新发送”。发不出卡时退回一行字。旧版本留下的、次数用完又没有 `resend_card` 的批次，`check_held` 补发一张。
- **记录**：`<state>/proxy/resend/<批次 id>.json`（card_id、message_id、sequence、视图）；批次另存 `resend_card`。sequence 不放批次，免得后台线程整份写批次时写回旧值。每次写都是整卡（`replace_form_card`，卡锁内 `reserve`）。
- **点击**（`click`、`decide`，经 `PANELS` 表）：非主人写在卡上；`Proxy.lock` 内重读批次：已作废、已发出、可能已发出、已跳过的画成对应结果；正在发或已请求过的不重复；卡上的原文和批次的不同时拒绝；关闭、暂停、移出或满了 `HOURLY_LIMIT` 时写明原因。都过了就写 `resend_requested`、`resend_by`，`attempts` 清零，锁外画成“正在重新发送…”。
- **发送**：仍在后台线程，`check_held` 见到 `resend_requested` 就照原样发 `batch["reply"]`（不跑模型、同一个幂等键），不看 `STALE_AFTER`。结果画在卡上：已发出、可能已发出（不再有按钮）、还是没发出（按钮恢复）。`check_held` 经 `Proxy.update` 写批次：锁内按磁盘上的合并，点击写的请求不会被盖掉。
- **收尾**：批次不经发送就结束时（`superseded`、`skipped`、主人那一轮无需回复），`ProxyResend.close` 把卡画成结果，去掉按钮。

## 代发

话题要以主人身份给别人发飞书消息时（通知同事、把结果交给某人），不经 lark-cli（主人的 `im:message.send_as_user` 可能被组织策略禁止，机器人也未必对对方可见），而是出一张代发草稿卡，主人确认后经同一个[发送器](#发送器)发出。`app/owner_send.py`（`Proxy.owner_send`），记录 `core/owner_send.py`，卡 `cards/owner_send.py`。

- **开关**：有发送器（`proxy_send.available()`）且 `COVEY_OWNER_SEND` 不是 `0` 时开（`enabled`）。关着时每轮不附规则；检查点里写了草稿也只在话题里回一行“这个部署不能以你的身份发”。和代答的开关无关：代答关着也能代发，不加白名单、不占代答的每小时限额。
- **话题怎么发起**：开着时每一轮的消息后附 `core/owner_send.py` 的 `RULE`（`turn_note`，代答话题和管家的私聊不附）：检查点写 `send_as_owner`：`[{"id","to":{"email"|"name"|"open_id"|"chat_id"},"text" 或 "text_file","why"}]`，至多 `MOST`（3）条；正文有密钥时写 `text_file`（绝对路径，至多 64 KB），不进检查点。
- **什么时候出卡**（`collect`，在 `app/detached.py`、`app/job_dispatch.py` 任务结束后调）：这一轮 `done` 或 `waiting_user`、回复已授权（`verified_reply_authorized`，规则 5）。一条草稿一个记录 `<state>/owner-send/<id>.json`（0600，目录 0700），id 由任务键和草稿 id 算出，同一轮读两次只出一次卡；同一话题同一草稿 id 还没发的旧稿转 `superseded`。
- **收件人**（`resolve`，用 `COVEY_PROXY_PROFILE` 的主人身份）：`chat_id` 查会话名；邮箱精确匹配；`open_id` 只认主人应用的（@ 消息里的 open_id 是机器人应用的，查不到时退到姓名）；姓名一人直接用，多人时卡上列至多 5 个候选点选，没人时草稿转 `declined`，卡上写原因。
- **卡**：在本轮所在话题的回复串里，另在聊天顶层发一行“有一条…草稿等你确认（到话题里看 ›）”。卡上是收件人（姓名 · 部门 · 邮箱）、为什么发、可编辑的正文框和「发送」「不发送」。kind `bridge_owner_send`、`bridge_owner_send_drop`、`bridge_owner_send_pick` 在 `ACTIONS`，经 `PANELS` 的 `proxy.owner_send` 认领；整卡替换（`replace_form_card`）。
- **点击**（`click`、`decide`）：非主人写在卡上；记录锁（`resource_lock("owner-send", …)`）内只读写记录：按钮值里的正文摘要和记录的不同时拒绝（卡已换过），框里是空的或超过 `TEXT_LIMIT` 拒绝，否则存下框里的正文（`edited` 记是否改过）转 `sending`，回执后（`after_ack`）发送。
- **发送**（`send`）：等 `Proxy.web_lock` 至多 `WEB_WAIT`（120 秒，和代答的发送、扫码登录互斥），拿不到算没发出。草稿充当联系人和批次交给 `ProxySend.web_send`，幂等键同代答（批次 id 加正文摘要：同一正文重试同一个键）。有回执是 `sent`；确定没发出（`validation`、`unsupported`、`auth`、`failed`、`launch`、拿不到锁）是 `unsent`，卡上出「重新发送」，`auth` 时告诉主人登录失效（`login_gone`）；其余可能已发出的先按会话读回，读不到是 `uncertain`，终态，不再发（规则 1、6）。结果画在卡上，话题里一行。
- **结果回话题**：话题之后的轮次在 `RULE` 后附最近 24 小时结束的代发结果（`turn_lines`，至多 3 条）。任务进程只读这些记录。
- **状态**只按 `TRANSITIONS` 前进（规则 4）：`drafted → sending | declined | superseded`，`sending → sent | uncertain | unsent`，`unsent → sending | declined`。
- 日志：`owner send drafted draft=… state=…`、`owner send confirmed draft=…`、`owner send draft=… status=… type=…`，发送器自己的 `proxy send batch=…` 一行。

## 网页端与浏览器服务

代答发送不经网页端，也不用浏览器服务：发送器自己扫码登录、直接调接口发。`bin/covey_feishu_web.py` 和 `feishu/web_client.py`、`feishu/web_service.py` 只给飞书网页端的验收页面用，见[测试与验收 · 飞书网页端的验收工具](testing.md#飞书网页端的验收工具)。

## 记录与清理

`<state>/proxy/`，只有 Bridge 写（字段说明在 `core/proxy.py` 的模块文档）：

| 路径 | 内容 |
| --- | --- |
| `settings.json` | 开关、代答群、用户 profile 和主人 ID、新话题的 Harness/模型/预算、`model_changed_at` |
| `contacts/<sha>.json` | 一个白名单会话：`kind`、`target`、`chat_id`、`paused`、`since`、`seen`、话题、`source`、`added_at` |
| `batches/<id>.json` | 一个批次（见「批次」） |
| `panels/`、`resend/` | 设置卡、重新发送卡的记录 |
| `style.md` | 主人的说话风格，附在每个批次的提示词后 |
| `owner-context.md` | 主人在做的事（项目和术语，主人确认能对外说的），附在风格说明前；没有这个文件就不附 |
| `send_login.json` | 已告诉主人的登录失效 |
| `send-as-user/` | 发送工具的 HOME：凭证、幂等记录 |
| `tmp/` | 发送的正文、登录二维码副本 |
| `web/` | 验收浏览器的 profile |

- 联系人和批次每次轮询、每次画卡都整个目录读，`core/proxy.py` 按目录缓存：本模块的写清缓存，别人的写看目录 mtime。
- 维护（`core/maintenance.py`）：`PROXY_BATCH_DAYS`（30）天没动、已结束的批次删掉，没结束的留着；`proxy/shots/` 按临时目录的规则清；验收浏览器的缓存每小时检查一次，超过 `WEB_CACHE_BYTES` 且没有 `sending` 批次、浏览器没开时删 `WEB_CACHES`（`ProxyLogin.prune_web_caches`），cookie 和登录数据不动。
- 每日备份只带 `proxy/settings.json`、`proxy/contacts/`、`proxy/style.md`、`proxy/owner-context.md`。

## 排障要点

| 现象 | 看哪里 |
| --- | --- |
| 代答卡顶部有警告 | `ProxySend.problems`：没装 uv、工具和安装时不同（重跑扩展里的发送工具安装脚本）、登录失效（点“去登录”） |
| 卡上写没有 profile | 服务没设 `COVEY_PROXY_PROFILE`，或那个 lark-cli profile 没有用户登录 |
| 对方发了消息但没有批次 | 日志 `proxy take-in failed source=…`（读取在退避）、`proxy search truncated`；联系人是否暂停；群消息是否 @了主人 |
| 话题里跑了但没发 | 批次的 `status` 和 `history`：`held` 的原因（暂停、`STALE_AFTER`、`HOURLY_LIMIT`、`LATER_TURN`、`SKIP_MARK`）；`proxy batch=… waits` 是登录占着 `web_lock` |
| 回复可能发了两次 | 不会自动重发：`uncertain` 是终态；同一回复的重试用同一个幂等键 |
| 一直“没发出” | 重新发送卡上的原因；`proxy send batch=… type=…` 的类型；`auth` 就重新登录 |
| 登录恢复后没补发 | 日志 `proxy send tool login is back`；补发仍要过发前闸门 |
| 批次推进报错 | `proxy batch=… failed: …`，只影响这一个批次 |

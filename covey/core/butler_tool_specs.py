"""The Bridge tools the butler's session gets (docs/butler.md): name, the description the model reads
(Chinese, like the owner's other tool notes) and the arguments' JSON schema. The MCP server lists these
(tools/list); the Bridge's handlers take the same arguments (app/butler_tools.py, app/butler_search.py,
app/butler_topics.py, app/butler_panel.py). Standard library only.

Every handler answers {"ok": bool, "text": what the model reads, "data": {...}}; a topic is named by its number
("T12", "412") or its topic id.
"""
from __future__ import annotations

from covey.core import ask_kinds
from covey.core import env as cenv
from covey.core.checkpoint import ASK_SHORT
from covey.core.process_env import env_flag

TOPIC = {"type": "string", "description": "话题：T 号（如 T412）或 topic_id"}
# What a handoff need not say (core/checkpoint.py ASK_RULE): every topic is told when to ask the owner. Short: it is on
# topic_start and topic_fork, which agy loads up front within backends/agy/home.py EAGER_BYTES.
ASK_NOTE = "什么时候问主人话题自己知道，不用写“严禁发问题卡”“别问我”"
TASK = ("交给话题的任务说明：做什么、范围、约束，用主人的说法；不含路由用语（开话题、放到哪个 T 号、转给谁）和本机路径。"
        "主人原话和附件由 Bridge 附上。" + ASK_NOTE)
ATTACHMENTS = {"description": "可选，交给它哪些附件：all 全部、none 不给、或序号列表（如 [\"图片 1\", \"文件 2\"] 或 [1, 3]），"
                              "补充消息的附件接着数（第一条有图片 1，补充里的图片 1 就是图片 2）；不填：填了 message 给那条的，"
                              "否则全给。拆成几件事时用它分附件",
               "anyOf": [{"type": "string", "enum": ["all", "none"]},
                         {"type": "array", "items": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string"}]}}]}
# 一轮几条消息 (T957): the owner's messages of the butler's turn, the one it started on and each supplement after it, counted
# from 1; a call that names one hands on that message's words (and, with no ``attachments``, its files) alone, and the topic it
# opens hangs under it (app/butler_handoff.py only, app/butler_topics.py said_message). topic_start's says it whole, the
# others point at it: agy's room (backends/agy/home.py EAGER_BYTES).
MESSAGE = {"type": "integer", "minimum": 1,
           "description": "可选，这件事出自主人这一轮第几条消息（开头那条是 1，补充的接着数）：只带那条的原话，话题挂在那条下面。"
                          "一轮里几条消息说的是不同的事时填"}
MESSAGE_SHORT = {"type": "integer", "minimum": 1, "description": "可选，同 topic_start 的 message"}
# A lead's project_topic_start and project_topic_send: the owner's files it got go on to the topic it names them for, none
# unnamed (T856: 转不转给话题由负责人决定). Short: agy's room for a lead's tools.
LEAD_ATTACHMENTS = {"type": "array", "items": {"type": "string"}, "description": "可选，转给它的附件，如[\"图片 1\"]或[\"all\"]；不写不转"}
# project_send's: which of the owner's files go to the lead (WAVE12 W2-B: one message for two projects or a project and a
# topic). Short, as the lead's: agy's room for the butler's tools.
SEND_ATTACHMENTS = {"type": "array", "items": {"type": "string"},
                    "description": "可选，主人的附件给负责人哪些，如[\"图片 1\"]或[\"none\"]；不写全给（填了 message 给那条的）"}
HARNESS_ARG = ("主人点名用哪个 Harness 时必须填（如“用 Claude Code”“让 Codex 做”），值写 Harness 名：claude、codex 等；"
               "主人没点名就不填，用设置里的默认")
MODEL_ARG = "主人点名用哪个模型时必须填，写模型名；主人没点名就不填，用设置里的默认"
EFFORT_ARG = "主人点名思考预算时必须填（如“用高预算”“max”）；主人没点名就不填，用设置里的默认"
# A preference holds for the kind of work it was said for (T786: "监视进程这类用 Codex" had other topics opened on Codex): a call
# that names a Harness, model or budget says on what (app/config_basis.py checks it). Short: agy's room (backends/agy/home.py).
BASIS_ARG = {"type": "string",
             "description": "填 harness/model/effort 时必填：主人原话里的一段或原则、记忆编号；原则只管它说的那类事，会核对"}
DONE = "结论由 Bridge 发给主人，你不用等、也不用转述。"
# 派活门槛, what goes to a topic, said in one wording per mode: DISPATCH_OWN in topic_start's description (START_OWN) and in
# app/prompts.py SHARED_RULES; with the butler's rules on (COVEY_BUTLER_RULES, unless 0) DISPATCH_BUTLER in START_BUTLER and
# in app/prompts.py BUTLER_RULES, which says it once (its second line no longer repeats the list).
DISPATCH_OWN = "可能超过一两分钟的、要改代码或文件的、主人说开话题的"
DISPATCH_BUTLER = "要查一圈的、要跑命令的、要改代码或文件的、要联网或多步操作的、预计超过半分钟的、主人说开话题的"
START_OWN = f"（{DISPATCH_OWN}），而不是在自己这一轮里做；只读、一两分钟能做完的查询和命令你自己做。"
START_BUTLER = (f"（{DISPATCH_BUTLER}），不要先自己试一试；只有几秒能答完的快答才自己答（怎么算快答见管家规则），"
                "拿不准就派出去，主人明确说“你直接做”时例外。")
# 完成标准 (core/criteria.py): each turn of the topic marks them, its answer card shows those not met.
CRITERIA = {"type": "array", "maxItems": 4, "items": {"type": "string"},
            "description": "可选，做完的标准，1 到 4 条，每条一句话：主人说的，或这件事做完必须满足的（如“全量测试通过”"
                           "“合进 main 并推送”）。话题每轮结束逐条核对，没满足的显示在答案卡上。问答、查询、调研不写"}
CRITERIA_CHANGE = {"type": "array", "maxItems": 4, "items": {"type": "string"},
                   "description": "可选，主人要改这个话题的完成标准时给出新的 1 到 4 条（给空数组就去掉）；不改不填"}

# 主目录启动 (docs/configuration.md 主目录与常用路径): a topic starts in the home and names the repositories it uses (the repo
# map's names, core/repomap.py); ``workspace`` is an older session's argument, taken as one repository read. Short: agy's room.
REPOS_ARG = {"type": "array", "maxItems": 10, "description": "可选，要用的仓库（repos_list 的名字），write 是要改它",
             "items": {"type": "object", "required": ["name", "mode"], "properties": {
                 "name": {"type": "string"}, "mode": {"type": "string", "enum": ["read", "write"]}}}}
WORKSPACE_ARG = {"type": "string", "description": "旧参数，同 repos 里只读的一项"}
# Whether a topic uses this machine's repositories and files (topic.json ``origin.local``, app/home_turn.py local): set when
# it opens, never changed after; left out, true.
# Short: agy's room (backends/agy/home.py EAGER_BYTES).
LOCAL_ARG = {"type": "boolean", "description": "只解读、调研论文、网页、飞书文档或写报告，用不上本机仓库和文件时填 false"}
WHY_ARG = {"type": "string", "description": "一行中文：为什么这样配置"}
# 派活时挑带 (docs/butler.md, app/memory_picks.py): the owner's 资料 the topic is to act on, by short code, from the 全局
# 「相关时才带」 the butler's and a lead's rules carry (app/prompts.py POOL_HEAD says what to pick) and a lead's project; the
# Bridge checks each and carries those it takes whole at the tail of the topic's turns. Short: agy's room (EAGER_BYTES).
MEMORY_ARG = {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "可选，“相关时才带”里要话题照做的短码"}

# 记忆 (docs/butler.md 记忆): one set of tools for the owner's 规矩 and 资料 (app/butler_knowledge.py memory_save,
# memory_revise, memory_search), always listed; a save quotes the owner's words of the turn (T908 M3a). The principle_*
# and knowledge_* tools of older sessions are run as these (RENAMED_TOOLS).
MEMORY_KINDS = ("rule", "note")
MEMORY_SCOPES = ("global", "project", "topic")
MEMORY_TOOLS = (
    {"name": "memory_save",
     "description": "记一条记忆：主人说“以后……”“记住……”“记一下……”时用。kind：rule 规矩（以后都照做）、note 资料（事实、做法、坑，"
                    "相关时才带）；主人答过的题 Bridge 已记成决定，不用记。quote 照抄主人这一轮原话里说这件事的一段（一字不改），"
                    "核对不到就不记。scope 按内容选：只关某个专项的 project、只关某个话题的 topic；规矩要主人原话明说“所有”“以后都”“处处”"
                    "才 global，没明说的进待审核等主人采纳。写全局时：跨一切场景每一轮都照做的记规矩，只在某类事、某个对象上照做的记资料"
                    "（相关时才带，管家和负责人派活时挑给话题）。text 用主人的说法写一句，主人只管某类事时照写那一类，不写机密，"
                    "不写“今天”“现在”这类会过时的说法。一次性的要求（只对这件事、这一轮）不记，意思相同的不重复记。记完回复“已记下：……”。",
     "input_schema": {"type": "object", "required": ["text", "kind", "scope", "quote"], "properties": {
         "text": {"type": "string", "description": "一句话，200 字内"},
         "quote": {"type": "string", "description": "主人这一轮原话里说这件事的一段，一字不改"},
         "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
         "scope": {"type": "string", "enum": list(MEMORY_SCOPES)},
         "project": {"type": "string", "description": "scope 为 project 时写：专项 id 或名字"},
         "topic": {"type": "string", "description": "scope 为 topic 时写：话题 T 号或 id"},
         "title": {"type": "string", "description": "可选，标题，40 字内"},
         "why": {"type": "string", "description": "可选，为什么，200 字内"}}}},
    {"name": "memory_revise",
     "description": "改一条记忆（key 是短码或旧 id，如 R7K2M、p1a2b3c）：update 改正文或标题；supersede 用新的替代，旧的收进历史；"
                    "retire 不再成立或已办完，收进历史。先用 memory_search 找到 key，对不上是哪条时问主人。"
                    "主人定的规矩要带 quote（主人这一轮说要改它的原话，一字不改），核对不到就不改。改完回复改了哪条。",
     "input_schema": {"type": "object", "required": ["key", "action"], "properties": {
         "key": {"type": "string", "description": "记忆的短码或旧 id"},
         "action": {"type": "string", "enum": ["update", "supersede", "retire"]},
         "text": {"type": "string", "description": "update、supersede 的新正文"},
         "title": {"type": "string", "description": "可选，新标题，40 字内"},
         "quote": {"type": "string", "description": "改主人定的规矩时必填：主人这一轮原话里说这件事的一段，一字不改"}}}},
    {"name": "memory_search",
     "description": "查记忆（规矩、决定、资料）：query 写几个词或一句话，留空列最近的；每行是短码 · 类型 · 范围 · 正文。"
                    "查以前定过什么、某条记过没有时先用它，没找到再 topics_search。",
     "input_schema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "搜索词"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "最多几条，默认 10"}}}},
)

TOOLS = (
    {"name": "topics_list",
     "description": "列出主人私聊里的话题：等你 / 有结果没看 / 在做 / 最近的，每行是 T 号、标题、所在组、工作区、Harness、"
                    "在问什么或最后一句、更新时间。问进度、找相关话题、决定要不要转交时先用它；每条消息开头的快照已经有概况。",
     "input_schema": {"type": "object", "properties": {
         "group": {"type": "string", "enum": ["waiting", "result", "running", "recent", "all"],
                   "description": "waiting 等你，result 有结果没看（和看板同一口径：停住的、停止的也算，没看过的不论多久都在），"
                                  "running 在做，recent 最近 3 天动过的（默认），all 全部"},
         "query": {"type": "string", "description": "按 T 号、标题（首条消息的第一行）、工作区过滤；"
                                                    "按链接、路径、内容找是哪个话题做的，用 topics_search"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "最多几行，默认 20"}}}},
    {"name": "topics_search",
     "description": "按线索找是哪个话题的哪一轮做的：飞书文档链接或 token、文件路径、关键词，一次 1 到 5 条。"
                    "搜主人私聊里所有话题的每一轮（主人的话、结果、检查点的产物和做完的步骤等），不只标题和首条消息。"
                    "每条命中是 T 号 · #轮次 · 时间 · 怎么出现的（第一次出现：之前的轮次都没有，多半是这一轮做的；"
                    "结果里也有：主人给过或之前出现过，这一轮的结果里也有；只是提到：只在主人的话里）· 片段。"
                    "主人说“之前话题给的文档/文件/结果”时先用它，链接和路径比关键词准；wiki 链接没命中时，"
                    "先查出它对应的 docx token 再搜，或读文档取标题里的词再搜。两个以上话题都像时问主人。",
     "input_schema": {"type": "object", "required": ["clues"], "properties": {
         "clues": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string"},
                   "description": "线索：飞书链接（取其中的 token）、token、文件路径、关键词"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "最多列几轮，默认 10"}}}},
    {"name": "topic_read",
     "description": "读一个话题最近几轮：主人的问题、结论（结果的开头）、检查点（阶段、做完的步骤、产物、下一步、在等主人什么、"
                    "每一步的状态和开始、做完的时间与用时、提交/部署/自检），"
                    "以及没回答的问题和选项。给 job 读指定的那一轮（如 topics_search 找到的），结果给得更长。"
                    "回答进度问题或替主人答题前用它；不要去读话题的原始记录文件。",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "topic": TOPIC,
         "job": {"type": "string", "description": "可选，轮次编号（如 2906 或 #2906）：读这一轮，turns 是到它为止的几轮"},
         "turns": {"type": "integer", "minimum": 1, "maximum": 5,
                   "description": "几轮，默认最近 2 轮；给了 job 时默认 1"}}}},
    {"name": "repos_list",
     "description": "列出常用路径：名字、状态（可写/只读/冻结）、用途、路径、最近用它的话题。开话题要写 repos 前用它。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "topic_start",
     "description": "为一件新的事开一个话题去做" + START_OWN +
                    "话题挂在主人这条提问下面；和已有话题是同一件事时不要新开，用 topic_send。"
                    "prompt 是交给话题的任务说明：用主人的说法写清楚要做什么、范围和主人提的约束（路径、别做什么、怎么交付），"
                    "可以补指代的是哪个话题、之前定过什么（最多 200 字，只写对话里的事实）。不写对你说的话，"
                    "不加你自己的计划和验收标准；主人原话和附件 Bridge 会附上。"
                    "话题从主目录直接开工，马上返回，不要等它做完：进展在之后消息的快照里。"
                    + DONE,
     "input_schema": {"type": "object", "required": ["title", "prompt", "user_requested", "why"],
                      "properties": {
         "title": {"type": "string", "description": "话题的标题，说是哪件事：中文、中文标点（：，不用半角 : ,），20 字以内"},
         "prompt": {"type": "string", "description": TASK},
         "attachments": ATTACHMENTS,
         "message": MESSAGE,
         "repos": REPOS_ARG,
         "workspace": WORKSPACE_ARG,
         "local": LOCAL_ARG,
         "memory": MEMORY_ARG,
         "user_requested": {"type": "boolean", "description": "主人在最新这条消息里亲口要求做这件事（直接下令、“去做”“开个话题”）为 true；你自己推断的为 false"},
         "why": WHY_ARG,
         "criteria": CRITERIA,
         "harness": {"type": "string", "description": HARNESS_ARG},
         "model": {"type": "string", "description": MODEL_ARG},
         "effort": {"type": "string", "description": EFFORT_ARG},
         "basis": BASIS_ARG}}},
    {"name": "topic_send",
     "description": "把主人的话追加给一个已有话题（同一件事，几天前的也算）：它正在跑就作为补充，已经停了就开它的下一轮。"
                    "text 写这次交给它的内容，用主人的说法；主人原话和附件 Bridge 会附上。是哪个话题照管家规则判断，"
                    "两个以上都像时先问主人。在回复里写明交给了哪个话题。" + DONE,
     "input_schema": {"type": "object", "required": ["topic", "text"], "properties": {
         "topic": TOPIC,
         "text": {"type": "string", "description": "交给话题的内容：主人这次要它做或补充的事，不含路由用语和本机路径"},
         "note": {"type": "string", "description": "可选，一句背景（最多 100 字）：写在你卡上“转给”那一行，也交给话题的模型；"
                                                   "不写路径，不写给主人看的话"},
         "title": {"type": "string", "description": "可选，开下一轮时任务卡上的标题，中文，20 字以内；不填用话题原来的标题"},
         "criteria": CRITERIA_CHANGE,
         "attachments": ATTACHMENTS,
         "message": MESSAGE_SHORT,
         "memory": MEMORY_ARG}}},
    {"name": "topic_fork",
     "description": "从一个已有话题分叉出新话题：新话题带着原话题的背景，原话题不受影响。相关但不是同一件事、又需要原话题的上下文时用。",
     "input_schema": {"type": "object", "required": ["topic", "title", "prompt"], "properties": {
         "topic": TOPIC, "title": {"type": "string", "description": "新话题的标题，说是哪件事：中文、中文标点，20 字以内"},
         "prompt": {"type": "string", "description": TASK},
         "attachments": ATTACHMENTS,
         "message": MESSAGE_SHORT,
         "memory": MEMORY_ARG}}},
    {"name": "question_answer",
     "description": "主人说了怎么答某个话题的选择题时（如“T412 选 A”），替他答掉。只认选择题：每题都要答，选项必须是列表里的原文，"
                    "多选给列表；自由文字的回答不行，改用 topic_send 作为补充。答完告诉主人答了什么。",
     "input_schema": {"type": "object", "required": ["topic", "answers"], "properties": {
         "topic": TOPIC,
         "answers": {"type": "object", "description": "题目 id（或题目原文）→ 选项原文；多选的给数组",
                     "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}}}}},
    {"name": "topic_priority",
     "description": "主人说某个话题急时，把它在看板里本组内提到最前（不打乱“等你 → 在做 → 有结果”的先后）。"
                    "主人只要把某个话题从看板撤下时，给 remove: true，把它从看板和快照里撤下；话题本身不动、任务照跑，"
                    "它有新问题或新结果时会自己回来。主人要它不再做的，用 topic_stop。",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "topic": TOPIC, "remove": {"type": "boolean", "description": "true：从看板撤下，而不是提前"}}}},
    # W0-7 (T892): the butler stops an ordinary topic as a lead stops a member (app/butler_tools.py topic_stop).
    {"name": "topic_stop",
     "description": "主人要某个普通话题不再做时（“T412 停掉”“别做了”“不要了”），真停掉它：排队和在跑的任务停下，"
                    "等主人回答的问题和没开工的启动卡收掉；话题还在，主人在里面说一句就接着做。只从看板撤下、任务照跑的用 topic_priority。"
                    "专项的话题不能用它停：用 project_send 交给负责人，写明停哪个。",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "topic": TOPIC, "why": {"type": "string", "description": "可选，一句为什么停（用主人的说法，120 字内），写进收掉的问题和任务"}}}},
    *MEMORY_TOOLS,
    {"name": "settings_panel",
     "description": "主人要看或改管家的设置时，在私聊里发出设置卡；设置只能由主人在卡上改，你不能改。",
     "input_schema": {"type": "object", "properties": {}}},
    # 批量换配置 (app/batch_switch.py, T786): a card the owner confirms; nothing is switched by the call itself. A lead has it too
    # (LEAD_SHARED), for its project's topics only.
    {"name": "topics_switch",
     "description": "主人要把几个已有话题一起换 Harness、模型或思考预算时用：Bridge 在私聊发一张批量换配置卡，逐个列出从什么换到什么，"
                    "主人点「确认更换」后才换，你不能直接换。不填的项沿用话题原来的（换了 Harness 用设置里的默认）。"
                    "发出后告诉主人卡在私聊里、点了才换。负责人只能换本专项的话题。",
     "input_schema": {"type": "object", "required": ["topics", "basis"], "properties": {
         "topics": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string"}, "description": "话题的 T 号"},
         "harness": {"type": "string", "description": "换到哪个 Harness：claude、codex 等"},
         "model": {"type": "string", "description": "换到哪个模型"},
         "effort": {"type": "string", "description": "思考预算"},
         "basis": {"type": "string", "description": "主人要换配置的原话里连续的一段，Bridge 会核对"}}}},
)

NAMES = tuple(tool["name"] for tool in TOOLS)

# Tools only some deployments have. The MCP server lists them when its environment says so (``listed``;
# core/butler.py mcp_config hands the variable on from the Bridge's own): without it a tool is not in the list, and a
# call to it is refused. HISTORY_ENV is core/knowledge.py's HISTORY_ENV. COVEY_KNOWLEDGE lists nothing here (the memory tools in
# TOOLS took the knowledge tools' place; an older session's knowledge_* calls are the Bridge's, app/butler_memory.py), so the
# MCP server is not handed it.
HISTORY_ENV = "COVEY_HISTORY_CMD"
HISTORY_TOOLS = (
    {"name": "history_search",
     "description": "搜 Claude、Codex 和 Bridge 的全部历史轮次（主人的一句请求加之后做的事）：问以前做过的事、之前的做法，"
                    "memory_search 和 topics_search 都没找到时用。query 写一句话描述；返回命中的轮次：时间、工作区、T 号、请求、"
                    "做了哪些步骤、结论开头和原文位置。最多等 70 秒；超时或没有这个命令会直接说。",
     "input_schema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "一句话描述要找的事"},
         "workspace": {"type": "string", "description": "可选：只查这个工作区"},
         "since": {"type": "string", "description": "可选：只查最近多久，如 30d（天数）"}}}},
)
ALL_NAMES = NAMES + tuple(tool["name"] for tool in HISTORY_TOOLS)


# 管家 (COVEY_BUTLER_RULES=1, app/prompts.py BUTLER_RULES): topic_start's description says what the rules say, not that
# the butler does a minute or two's work itself. The name is app/prompts.py's BUTLER_RULES_ENV.
BUTLER_RULES_ENV = "COVEY_BUTLER_RULES"
assert any(tool["name"] == "topic_start" and START_OWN in tool["description"] for tool in TOOLS)


def butler_tools(tools) -> tuple:
    """``tools`` with topic_start's description as the butler's rules say it (START_BUTLER in place of START_OWN)."""
    return tuple({**tool, "description": tool["description"].replace(START_OWN, START_BUTLER)}
                 if tool["name"] == "topic_start" else tool for tool in tools)


# 专项 (COVEY_PROJECTS=1, docs/architecture.md 专项): the tools of a project's lead (app/project_tools.py, app/project_lead.py).
# The server lists them, and only them besides LEAD_READ, when COVEY_PROJECT_LEAD is set in its environment (the Bridge sets
# it for a lead's job alone, core/butler.py mcp_config); the butler's list never has them. ``thread`` names a
# thread of this project: T number or topic id.
LEAD_ENV = "COVEY_PROJECT_LEAD"
THREAD = {"type": "string", "description": "专项的话题：T 号（如 T412）或 topic_id，要是这个专项的话题"}
# The deploy approval a lead hands a topic with (app/project_approvals.py): the Bridge checks it and heads the handoff with it.
APPROVAL_ARG = {"type": "string", "description": "可选，派部署时填部署授权凭证（如 A3）"}
# The lead's questions to the owner (WAVE12 C4): written into its turn's checkpoint, a question card at the turn's end.
ASK_MAX = 4
PROJECT_ASK = {
    "name": "project_ask",
    "description": f"问主人要他定的事（{ASK_SHORT}，范围、取舍，或主人说过要自己定的）：这一轮结束时 Bridge 发问题卡、进“等你”，"
                   "任何汇报风格都发，主人答了会接着叫你。一次最多 4 题，你之前问了还没答的会并进来。"
                   "部署一批时 kind 写 deploy、填 deploy；主人同意后 Bridge 给授权凭证，派部署时用 approval 带上。",
    "input_schema": {"type": "object", "required": ["questions"], "properties": {
        "summary": {"type": "string", "description": "一句话：要主人定什么"},
        "questions": {"type": "array", "minItems": 1, "maxItems": ASK_MAX, "items": {
            "type": "object", "required": ["id", "question", "kind"], "properties": {
                "id": {"type": "string"},
                "header": {"type": "string"},
                "question": {"type": "string"},
                "context": {"type": "string"},
                "kind": {"type": "string", "enum": list(ask_kinds.QUESTION_KINDS)},
                "multiple": {"type": "boolean"},
                "options": {"type": "array", "items": {"type": "object", "required": ["label"], "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "grants_approval": {"type": "boolean", "description": "kind 为 deploy 时：选它即同意部署"}}}},
                "deploy": {"type": "object", "properties": {
                    "commits": {"type": "array", "items": {"type": "string"}},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "target": {"type": "string"}}}}}}}},
}
PROJECT_TOOLS = (
    {"name": "project_topic_start",
     "description": "为这个专项开一个话题去做一件事，马上返回，不要等它做完：话题结束后会有下一轮叫醒你。"
                    "prompt 是交给话题的任务说明，写法见负责人规则。话题从主目录直接开工。",
     "input_schema": {"type": "object", "required": ["title", "prompt", "why"], "properties": {
         "title": {"type": "string", "description": "话题的标题，说是哪件事：中文、中文标点，20 字以内"},
         "prompt": {"type": "string", "description": "交给话题的任务说明。" + ASK_NOTE},
         "repos": REPOS_ARG,
         "workspace": WORKSPACE_ARG,
         "local": LOCAL_ARG,
         "memory": MEMORY_ARG,
         "attachments": LEAD_ATTACHMENTS,
         "user_requested": {"type": "boolean", "description": "主人在这个专项里亲口要求做这件事为 true；你自己推断的为 false"},
         "why": WHY_ARG,
         "criteria": CRITERIA,
         "harness": {"type": "string", "description": HARNESS_ARG},
         "model": {"type": "string", "description": MODEL_ARG},
         "effort": {"type": "string", "description": EFFORT_ARG},
         "basis": BASIS_ARG,
         "approval": APPROVAL_ARG}}},
    {"name": "project_topic_send",
     "description": "给一个已有话题加话：它正在跑就作为补充，已经停了就开它的下一轮。text 写要它补做或改的事。"
                    "可以是这个专项的话题，也可以是主人私聊里还不在任何专项的普通话题：那样它先被收进这个专项（在跑的任务照跑），"
                    "再加话。别的专项的话题、负责人话题、主人移出过专项的话题用不了。",
     "input_schema": {"type": "object", "required": ["thread", "text"], "properties": {
         "thread": {"type": "string", "description": "话题的 T 号（如 T412）或 topic_id：这个专项的，或还不在任何专项的普通话题"},
         "text": {"type": "string", "description": "交给话题的内容：要它做或补充的事"},
         "note": {"type": "string", "description": "可选，一句背景（最多 100 字）"},
         "attachments": LEAD_ATTACHMENTS,
         "memory": MEMORY_ARG,
         "approval": APPROVAL_ARG}}},
    {"name": "project_topic_stop",
     "description": "主人要停这个专项里的某个话题时用：停掉它排队和在跑的任务，也收掉它还在等主人回答的问题（话题不再等回答），"
                    "话题仍记在专项里、算结束。只能停这个专项自己的话题；停掉的话题要接着做，再用 project_topic_send 给它加话。"
                    "why 写主人为什么要停（可选，一句话）。",
     "input_schema": {"type": "object", "required": ["thread"], "properties": {
         "thread": THREAD, "why": {"type": "string", "description": "可选，一句话：主人为什么要停"}}}},
    {"name": "project_note",
     "description": "在专项卡上记一句进展（最多 600 字）。report 为 true 的要告诉主人：Bridge 按风格把这句原文发给他（里程碑和完成、"
                    "话题结束、或详细时才发）；kind：milestone 里程碑，thread 话题的结果，note 一般进展。要主人定的事用 project_ask。"
                    "decision 为 true 时也记进决定日志（之后每一轮都看到最近 10 条）。别每步都记。",
     "input_schema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string", "description": "一句话，最多 600 字"},
         "report": {"type": "boolean", "description": "要告诉主人为 true；默认 false，只放在卡上"},
         "kind": {"type": "string", "enum": ["milestone", "thread", "note"], "description": "默认 note"},
         "decision": {"type": "boolean", "description": "这是一条定下来的决定为 true"}}}},
    PROJECT_ASK,
    {"name": "project_goal",
     "description": "写下这个专项的目标和完成标准：给主人看的一两句（要做成什么、做到什么算完，最多 200 字），网页工作台的专项标题下就显示它。"
                    "第一轮写一次；主人改了要求、目标变了再更新。只写目标，不写进度、状态和派活的安排。",
     "input_schema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string", "description": "目标和完成标准，一两句，最多 200 字"}}}},
    {"name": "project_remember",
     "description": "记进专项记忆：本专项长期适用的规矩（rule）或资料（note：事实、联系人、做法）。"
                    "只记主人说过的或定下的，不写机密；同文不重复记；主人删掉的不再记回去。",
     "input_schema": {"type": "object", "required": ["kind", "text"], "properties": {
         "kind": {"type": "string", "enum": ["rule", "note"]},
         "title": {"type": "string", "description": "可选，标题，40 字内"},
         "text": {"type": "string", "description": "一句话，200 字内"}}}},
    {"name": "project_memory_revise",
     "description": "改专项记忆里已有的一条（id 是专项记忆每行开头〔〕里的）：口径变了、记错了、两条说的是一件事，都用它，"
                    "不要再追加一条“XXX 作废”。op：update 改这条的正文或种类（改之前的原文留在修改记录里）；"
                    "supersede 用新的替代旧的（text 写新条，或 by 写已有的一条的 id 来合并重复），旧的收进历史、不再每轮带上；"
                    "retire 这条不再成立或已办完，收进历史。不能改、替代或作废主人定的规矩和他留下的限制（要改请问主人）；"
                    "主人的决定不能改正文，办完了才能 retire，主人有了更新的答复才能被那条 supersede。",
     "input_schema": {"type": "object", "required": ["op", "id"], "properties": {
         "op": {"type": "string", "enum": ["update", "supersede", "retire"],
                    "description": "update 改正文或种类；supersede 用新条或已有的一条替代旧的；retire 收进历史"},
         "id": {"type": "string", "description": "要处理的那条的 id"},
         "text": {"type": "string", "description": "update、supersede 的新正文，一句话，最多 200 字"},
         "kind": {"type": "string", "enum": ["decision", "constraint", "contact", "fact"], "description": "可选，新的种类"},
         "by": {"type": "string", "description": "supersede 用：已有的一条的 id，由它替代旧的（合并重复）；和 text 二选一"},
         "why": {"type": "string", "description": "可选，一句话：为什么改（记进修改记录）"}}}},
    {"name": "project_style",
     "description": "主人说“少打扰我”“多报一点”这类时，改这个专项的汇报风格；只给要改的项。"
                    "report：ask_only 只在要主人定时说 / milestones 里程碑和完成 / each_thread 每个话题结束 / verbose 详细；"
                    "detail：一行 / 简 / 全。",
     "input_schema": {"type": "object", "properties": {
         "report": {"type": "string", "enum": ["ask_only", "milestones", "each_thread", "verbose"]},
         "detail": {"type": "string", "enum": ["一行", "简", "全"]}}}},
    {"name": "project_finish",
     "description": "目标和完成标准都达到、话题都结束了，收尾：专项记为做完。还有话题在跑或等主人时会拒绝；整理专项建出来、主人还没在里面说过话、你也没派过活的专项也会拒绝。"
                    "summary 写一句做成了什么（最多 600 字），会作为做完的汇报。",
     "input_schema": {"type": "object", "required": ["summary"], "properties": {
         "summary": {"type": "string", "description": "一句话：做成了什么"}}}},
)
# The lead's two tools of the question triage (COVEY_PROJECT_TRIAGE=1 in the server's environment: ``lead_tools``; app/triage.py):
# a member topic's choice question is held for the lead, which answers it or hands it back to the owner.
TRIAGE_ENV = "COVEY_PROJECT_TRIAGE"
TRIAGE_TOOLS = (
    {"name": "project_answer",
     "description": "替主人答专项里一个话题交给你的选择题（题目在“专项状态”的“交给你的话题问题”里）。只答能依据专项记忆和主人定过的事回答的；"
                    "每题都要答，答案必须是选项原文，多选的给数组。拿不准就用 project_escalate，不要猜。回答会注明是负责人替主人答的，"
                    "话题照常往下做，也记进专项卡的分流记录。",
     "input_schema": {"type": "object", "required": ["thread", "answers"], "properties": {
         "thread": THREAD,
         "answers": {"type": "object", "description": "题目 id（或题目原文）→ 选项原文；多选的给数组",
                     "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}}}}},
    {"name": "project_escalate",
     "description": "专项里一个话题交给你的问题，你答不了或不该替主人答，转给主人：问题卡马上发给主人，卡上带上你写的 why。"
                    f"拿不准、涉及{ASK_SHORT}或别的撤不回的操作，都转。3 分钟没人处理也会自动转给主人。"
                    "手机上主人只看得到这张卡：能写就写 context（这题在改什么）和 recommend（你推荐哪个选项，原文）、if_not（不选它的代价）。",
     "input_schema": {"type": "object", "required": ["thread", "why"], "properties": {
         "thread": THREAD, "why": {"type": "string", "description": "一句话：为什么要主人定"},
         "context": {"type": "string", "description": "可选，背景一句话：这题在改什么、为什么现在问（最多 120 字）"},
         "recommend": {"type": "string", "description": "可选，你推荐的选项（写选项原文）"},
         "if_not": {"type": "string", "description": "可选，一句话：不选推荐项的代价（写了 recommend 才有用）"}}}},
)
# 协作 (COVEY_COLLAB=1 in the server's environment, core/butler.py mcp_config hands it on; ``lead_tools``): the lead keeps
# the project's 目标检查表 (app/project_tools.py project_checklist, core/project_checklist.py). project_answer names its
# grounds (``evidence``, checked by the Bridge: app/triage.py grounds) with 协作 on or off (W0-2).
COLLAB_ENV = "COVEY_COLLAB"
CHECKLIST_ITEM = {"type": "object", "properties": {
    "id": {"type": "string", "description": "条目 id（如 c2）；新加的不填"},
    "criterion": {"type": "string", "description": "完成标准，一句话；新加的必填"},
    "title": {"type": "string", "description": "短标题"},
    "topic": {"type": "string", "description": "主要交付它的话题（T 号）；没派的不填，空串清掉"},
    "topics": {"type": "array", "items": {"type": "string"}, "description": "也参与的话题，整组替换，最多 6 个"},
    "needs": {"type": "array", "items": {"type": "string"}, "description": "依赖的条目 id，最多 6 个"},
    "evidence": {"type": "string", "description": "证据：提交号、产物路径、检查点自检；done 必填"},
    "gap": {"type": "string", "description": "还缺什么、下一步谁做；blocked 写卡在哪，dropped 写为什么不要"},
    # Not "": Gemini refuses a declaration with an empty enum value (agy's leads failed each turn with 400, T839).
    "waiting_on": {"type": "string", "enum": ["owner", "none"], "description": "下一步在主人手上写 owner，不在主人手上写 none"},
    "state": {"type": "string", "enum": ["open", "done", "blocked", "dropped"],
              "description": "open 没满足（默认）/ done 满足 / blocked 卡住 / dropped 不做了"}}}
ACTION_ITEM = {"type": "object", "properties": {
    "id": {"type": "string", "description": "动作 id（如 a3）；新加的不填"},
    "title": {"type": "string", "description": "动作名；新加的必填"},
    "note": {"type": "string", "description": "现状一句，80 字内"},
    "items": {"type": "array", "items": {"type": "string"}, "description": "条目 id，整组替换"},
    "topics": {"type": "array", "items": {"type": "string"}, "description": "话题 T 号，整组替换"},
    "state": {"type": "string", "enum": ["open", "dropped"]}}}
CHECKLIST_TOOLS = (
    {"name": "project_checklist",
     "description": "维护目标检查表的条目和简报的动作；缺口处理完用 close_gaps 写怎么处理的。不传参数是查看。",
     "input_schema": {"type": "object", "properties": {
         "items": {"type": "array", "items": CHECKLIST_ITEM, "description": "新加或改的条目，最多 20 条；改的只写要改的项"},
         "actions": {"type": "array", "items": ACTION_ITEM, "description": "新加或改的动作，写法同 items"},
         "close_gaps": {"type": "array", "items": {"type": "object", "required": ["gap", "how"], "properties": {
             "gap": {"type": "string", "description": "缺口 id（如 g1a2b3c）"},
             "how": {"type": "string", "description": "怎么处理的：谁补了、证据在哪，或主人说不用补"}}}}}}},
)
# 简报 (with COVEY_COLLAB=1 too, app/project_tools.py): the goal's background, and a topic opened or fed on an action or an item.
ACTION_ARG = {"type": "string", "description": "动作 id"}
ITEM_ARG = {"type": "string", "description": "条目 id"}
BACKGROUND_ARG = {"type": "string", "description": "背景，300 字内"}
EVIDENCE = {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"},
            "description": "替主人答题的依据：专项记忆条目的 id（状态包专项记忆每行开头〔〕里的，如 0123456789abcdef-2），"
                           "或主人在这个专项里的原话 msg:<消息 id>（状态包“主人在专项里说过的话”里的）。"
                           "只有主人说的、仍有效的才算依据；负责人记的、作废、做完、被替代或只限当次的会被拒绝，"
                           "被拒了就用 project_escalate 转给主人。"}
EVIDENCE_NOTE = "要写 evidence：Bridge 核对每条依据，没有主人说的、仍有效的依据就拒绝，回答记着依据，主人核对得到。"
PROJECT_NAMES = tuple(tool["name"] for tool in PROJECT_TOOLS + TRIAGE_TOOLS + CHECKLIST_TOOLS)
# How a project's lead asks the owner (WAVE12 C4, W1-A), as a backend's own words for a lead's turn say it
# (backends/codex/appserver.py ASK_LEAD, an extension's notes; W2-A).
LEAD_ASK_TOOL = "project_ask"
assert LEAD_ASK_TOOL in PROJECT_NAMES


def with_evidence(tool) -> dict:
    """project_answer as the lead sees it: ``evidence`` required, its description saying so; others as they are."""
    if tool["name"] != "project_answer":
        return tool
    schema = tool["input_schema"]
    return {**tool, "description": tool["description"] + EVIDENCE_NOTE,
            "input_schema": {**schema, "required": [*schema["required"], "evidence"],
                             "properties": {**schema["properties"], "evidence": EVIDENCE}}}
def with_brief(tool) -> dict:
    """The lead's project_topic_start and project_topic_send with ``action`` and ``item``, and project_goal with ``background``
    (neither text nor background required then), as the lead sees them with 协作 on; others as they are."""
    name, schema = tool["name"], tool["input_schema"]
    if name in ("project_topic_start", "project_topic_send"):
        return {**tool, "input_schema": {**schema, "properties": {**schema["properties"], "action": ACTION_ARG, "item": ITEM_ARG}}}
    if name == "project_goal":
        return {**tool, "input_schema": {**{key: value for key, value in schema.items() if key != "required"},
                                         "properties": {**schema["properties"], "background": BACKGROUND_ARG}}}
    return tool


# The lead's two tools were project_thread_start and project_thread_send in older releases, and the butler had workspaces_list,
# principle_* and knowledge_*. The list names only the new ones; a call under an old name (a session that began earlier, a
# server of an older release) is run as the new one, its arguments as they were (app/butler_tools.py).
RENAMED_TOOLS = {"project_thread_start": "project_topic_start", "project_thread_send": "project_topic_send",
                 # 主目录启动 and the one memory (docs/butler.md): no longer listed; app/butler_tools.py takes their arguments.
                 "workspaces_list": "repos_list", "principle_save": "memory_save", "knowledge_save": "memory_save",
                 "principle_delete": "memory_revise", "knowledge_update": "memory_revise", "knowledge_drop": "memory_revise",
                 "principles_list": "memory_search", "knowledge_search": "memory_search"}


def current_name(name):
    """``name``, or the new name of a tool the list no longer has under it (RENAMED_TOOLS)."""
    return RENAMED_TOOLS.get(name, name) if isinstance(name, str) else name
LEAD_READ = ("topics_list", "topic_read", "topics_search", "repos_list")  # What of TOOLS the lead has: it reads, the topics act.
# What else of TOOLS the lead has: topics_switch only proposes, on a card the owner confirms (app/batch_switch.py);
# memory_search reads its own project's memory only (WAVE12 W2-C: app/butler_memory.py memory_search keeps a lead's call to
# it). Both SECONDARY: agy names them, their schemas take nothing of the lead's up-front room.
LEAD_SHARED = LEAD_READ + ("topics_switch", "memory_search")


# 专项 for the butler (COVEY_PROJECTS=1 in the server's environment, which the Bridge hands on: core/butler.py mcp_config):
# topic_start and topic_send take ``project``, and projects_list and project_send are listed after the others (app/butler_projects.py).
# With the switch off the list is exactly what it was.
PROJECTS_ENV = "COVEY_PROJECTS"
START_PROJECT = {"type": "boolean", "description": "这件事要分成几个话题、跨几天持续推进（例如主人要长期跟进、反复推进），"
                                                   "或主人直接说“开个专项”“转成专项”时填 true：不开普通话题，开一个专项，由它的负责人去开专项的话题。"
                                                   "这时 title 是专项名，prompt 写目标和完成标准（主人的说法），"
                                                   "repos 是专项常用的仓库；"
                                                   "harness、model、effort 这时是负责人用的，主人点名负责人用哪个（如“负责人用 Codex”）才填；"
                                                   "主人要专项的话题用哪个模型，写进 prompt，负责人开话题时照办；拿不准就不要填"}
START_TOPICS = {"type": "array", "items": {"type": "string"},
                "description": "只在 project 为 true 时用：主人要收进这个专项的已有话题，T 号（如 T531）或话题 id，一个一项。"
                               "它们会直接挂进专项、交给负责人跟进，正在跑的不打断，等主人回答的问题仍归主人；"
                               "挂不上的（没找到、已在别的专项里）回复里会逐个说，要转告主人。只写在 prompt 里不会挂上"}
SEND_PROJECT = {"type": "boolean", "description": "主人要把这件事长期跟进、分成几个话题跨几天推进，或直接说“转成专项”“开个专项”时填 true："
                                                  "这个话题会转成专项（只在转成专项打开时生效）；一般的补充、拿不准的都不要填"}
BUTLER_PROJECT_TOOLS = (
    {"name": "projects_list",
     "description": "列出主人的专项：id、名字、状态（在做、停住、收起、做完、已取消；收起是没有要做的事放久了，有新的事会自动展开）、专项的话题的 T 号、负责人最近记的一句。在做的专项都列出（最近有动静的在前），停住和收起的列最近 30 个，做完的列最近 3 个。"
                    "主人问专项进展、要给专项加话、或要判断某件事是不是已有专项的一部分时先用它（每一轮的快照里也列着在做的和收起的专项）。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "project_send",
     "description": "把主人的话交给一个专项的负责人：它正在跑就作为补充，已经停了就开它的下一轮。新的事和某个专项是同一件事，就用它交给那个专项，收起的专项会随之重新展开；拿不准是不是同一件事，用 topic_start 开普通话题，不要硬归；主人说“这是 X 的事”就归进 X。专项的话题不能用 topic_send 转话，只能经负责人。"
                    "text 写这次交给它的内容：主人要改的目标、要加的事、对某个话题的意见，用主人的说法，不含路由用语和本机路径；"
                    "主人原话 Bridge 会附上。" + DONE,
     "input_schema": {"type": "object", "required": ["pid", "text"], "properties": {
         "pid": {"type": "string", "description": "专项的 id（projects_list 里的），或它名字里的几个字"},
         "text": {"type": "string", "description": "交给负责人的内容：主人这次要它做或改的事"},
         "attachments": SEND_ATTACHMENTS,
         "message": MESSAGE_SHORT,
         "topics": {"type": "array", "items": {"type": "string"},
                    "description": "可选：主人要收进这个专项的已有话题，T 号（如 T531）或话题 id，一个一项。直接挂进专项、交给负责人跟进，"
                                   "正在跑的不打断，等主人回答的问题仍归主人；挂不上的（没找到、已在别的专项里）回复里会逐个说，要转告主人"}}}},
)
# 整理专项 (COVEY_PROJECT_TIDY suggest or auto, with COVEY_PROJECTS=1; app/project_tidy.py): listed after the project tools.
TIDY_ENV = "COVEY_PROJECT_TIDY"
TIDY_TOOL = {
    "name": "projects_tidy",
    "description": "整理专项：把现有专项和最近 7 天动过的话题重新看一遍，在后台跑，结果是私聊里的一张“整理专项”卡（能归进已有专项的直接归，"
                   "新建、合并、拆开、改名等主人点「好」才做）。主人说“整理一下专项”“看看哪些该建专项”时用它；只整理某一部分时把那个词或专项 id 放进 scope。"
                   "正在整理时它只回现在读到第几个，不会再开一次。用了以后告诉主人已经开始、结果会发到私聊，不要自己列建议。",
    "input_schema": {"type": "object", "properties": {
        "scope": {"type": "string", "description": "只整理和这个有关的：一个词（如“Covey”）或专项 id；空就是全部"}}}}
BUTLER_PROJECT_NAMES = tuple(tool["name"] for tool in BUTLER_PROJECT_TOOLS) + (TIDY_TOOL["name"],)


def tidy_listed(environ) -> bool:
    """Whether 整理专项 is on in ``environ``: COVEY_PROJECT_TIDY unset, empty, ``suggest`` or ``auto``."""
    return (str(cenv.get(TIDY_ENV, "", environ) or "").strip().lower() or "suggest") in ("suggest", "auto")


def project_butler_tools(tools, tidy=False) -> tuple:
    """``tools`` with ``project`` on topic_start (and ``topics``, the existing topics a project takes in) and topic_send, and
    the projects_list and project_send tools last (and projects_tidy, ``tidy``)."""
    changed = []
    for tool in tools:
        extra = {"topic_start": {"project": START_PROJECT, "topics": START_TOPICS},
                 "topic_send": {"project": SEND_PROJECT}}.get(tool["name"])
        if extra is not None:
            schema = tool["input_schema"]
            tool = {**tool, "input_schema": {**schema, "properties": {**schema["properties"], **extra}}}
        changed.append(tool)
    return tuple(changed) + BUTLER_PROJECT_TOOLS + ((TIDY_TOOL,) if tidy else ())


# Only with COVEY_PROJECT_AUTOWAKE=1 (core/projects.py AUTOWAKE_ENV) does a topic's end wake the lead (app/project_loop.py
# autowake_due); without it project_topic_start says so (T647 P0-6: it promised the next turn all the same).
AUTOWAKE_ENV = "COVEY_PROJECT_AUTOWAKE"
WOKEN = "话题结束后会有下一轮叫醒你。"
NOT_WOKEN = "话题结束不会叫醒你（没开叫醒负责人）：它的回执留在收件箱，主人下次找你时再读。"
assert any(tool["name"] == "project_topic_start" and WOKEN in tool["description"] for tool in PROJECT_TOOLS)


def lead_tools(environ=None) -> tuple:
    """The lead's tools; the two of the question triage with COVEY_PROJECT_TRIAGE=1 in ``environ``, project_answer asking for its
    ``evidence`` (``with_evidence``, with 协作 on or off: W0-2); with COVEY_COLLAB=1 there also project_checklist and the 简报's
    arguments (``with_brief``). An ``environ`` without
    COVEY_PROJECT_AUTOWAKE=1 (the Bridge hands it on, core/butler.py mcp_config) has project_topic_start say no turn comes
    when its topic ends (NOT_WOKEN)."""
    triage = environ is not None and env_flag(TRIAGE_ENV, True, environ)
    collab = environ is not None and str(cenv.get(COLLAB_ENV, "", environ)).strip() == "1"
    asked = tuple(map(with_evidence, TRIAGE_TOOLS))
    tools = PROJECT_TOOLS
    if environ is not None and not env_flag(AUTOWAKE_ENV, True, environ):
        tools = tuple({**tool, "description": tool["description"].replace(WOKEN, NOT_WOKEN)}
                      if tool["name"] == "project_topic_start" else tool for tool in tools)
    return (tuple(tool for tool in TOOLS if tool["name"] in LEAD_SHARED) + (tuple(map(with_brief, tools)) if collab else tools)
            + (CHECKLIST_TOOLS if collab else ()) + (asked if triage else ()))


# The butler's tools a turn seldom needs. A Harness that fits a turn's tools in a budget (Antigravity, backends/agy/home.py)
# loads the others up front first and lists these by name once the room is taken: they work the same, the model reads
# one's schema when it calls it. Its budget is the skills' too, and over it agy drops the largest tool whole, which was
# topic_start (T750: the butler forked a finished topic in its place).
SECONDARY = frozenset({"topic_priority", "topic_stop", "settings_panel", "topics_switch", "repos_list",
                       "project_memory_revise",  # T852: a lead revises a line now and then; agy reads its schema when it calls it.
                       *(tool["name"] for tool in MEMORY_TOOLS + HISTORY_TOOLS), TIDY_TOOL["name"]})
# The tools that hand work to topics, what a turn of the butler or of a lead is for: such a Harness loads them first.
HANDING = frozenset({"topic_start", "topic_send", "topic_fork", "project_send", "project_topic_start", "project_topic_send",
                     "project_ask"})  # W1-A: a lead's question to the owner is what a wake-up turn is often for.
# A lead sets its report style now and then: agy reads project_style's schema when it calls it, which leaves the room
# project_topic_start's and project_topic_send's ``attachments`` take (T856).
SECONDARY |= {"project_style"}
# A lead writes its goal once, remembers a rule and stops a topic now and then: agy reads their schemas when it calls them,
# which leaves the room project_ask and ``approval`` take (W1-A, WAVE12 C4: the up-front room had 42 bytes left).
SECONDARY |= {"project_goal", "project_remember",
              "project_topic_stop"}  # Stopping a member topic is as seldom as the butler's topic_stop, which is one too.
assert SECONDARY | HANDING <= set(ALL_NAMES + BUTLER_PROJECT_NAMES + PROJECT_NAMES) and not SECONDARY & HANDING


def listed(environ) -> tuple:
    """The tools the MCP server lists in ``environ``: TOOLS (the memory tools among them), with history_search when
    COVEY_HISTORY_CMD is an absolute path; unless COVEY_BUTLER_RULES is off topic_start's
    description is the butler's (``butler_tools``). The Bridge hands the variable on (core/butler.py mcp_config).
    A project's lead (COVEY_PROJECT_LEAD set) has ``lead_tools`` and nothing else."""
    if str(cenv.get(LEAD_ENV, "", environ)).strip():
        return lead_tools(environ)
    extra = ()
    if str(cenv.get(HISTORY_ENV, "", environ)).strip().startswith("/"):
        extra += HISTORY_TOOLS
    tools = TOOLS + extra
    tools = butler_tools(tools) if env_flag(BUTLER_RULES_ENV, True, environ) else tools
    if not env_flag(PROJECTS_ENV, True, environ):
        return tools
    return project_butler_tools(tools, tidy_listed(environ))

"""The Bridge tools the main thread's session gets (docs/main-thread.md): name, the description the model reads
(Chinese, like the owner's other tool notes) and the arguments' JSON schema. The MCP server lists these
(tools/list); the Bridge's handlers take the same arguments (app/main_tools.py, app/main_search.py,
app/main_topics.py, app/main_thread_panel.py). Standard library only.

Every handler answers {"ok": bool, "text": what the model reads, "data": {...}}; a topic is named by its number
("T412", "412") or its topic id.
"""
from __future__ import annotations

from covey.core import env as cenv

TOPIC = {"type": "string", "description": "话题：T 号（如 T412）或 topic_id"}
TASK = ("交给话题区的任务说明：做什么、范围、约束，用主人的说法；不含路由用语（开话题区、放到哪个 T 号、转给谁）和本机路径。"
        "主人原话和附件由 Bridge 附上")
ATTACHMENTS = {"description": "可选，主人这条消息的附件交给它哪些：all 全部（默认）、none 不给、或序号列表（如 [\"图片 1\", \"文件 2\"]，"
                              "或按附件块里的先后写 [1, 3]）。这一轮里主人又补充了消息时，补充的附件接在后面连着数"
                              "（第一条有图片 1，补充里的图片 1 就是图片 2）。一条消息拆成几件事时，用它把附件分给对应的话题区",
               "anyOf": [{"type": "string", "enum": ["all", "none"]},
                         {"type": "array", "items": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string"}]}}]}
DONE = "结论由 Bridge 发给主人，你不用等、也不用转述。"

TOOLS = (
    {"name": "topics_list",
     "description": "列出主人私聊里的话题：等你回答 / 有结果未看 / 进行中 / 最近的，每行是 T 号、标题、所在组、工作区、Harness、"
                    "在问什么或最后一句、更新时间。问进度、找相关话题、决定要不要转交时先用它；每条消息开头的快照已经有概况。",
     "input_schema": {"type": "object", "properties": {
         "group": {"type": "string", "enum": ["waiting", "result", "running", "recent", "all"],
                   "description": "waiting 等你回答，result 有结果未看（和看板同一口径：停止的也算，没看过的不论多久都在），"
                                  "running 进行中，recent 最近 3 天动过的（默认），all 全部"},
         "query": {"type": "string", "description": "按 T 号、标题（首条消息的第一行）、工作区过滤；"
                                                    "按链接、路径、内容找是哪个话题做的，用 topics_search"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "最多几行，默认 20"}}}},
    {"name": "topics_search",
     "description": "按线索找是哪个话题的哪一轮做的：飞书文档链接或 token、文件路径、关键词，一次 1 到 5 条。"
                    "搜主人私聊里所有话题的每一轮（主人的话、结果、检查点的产物和已完成等），不只标题和首条消息。"
                    "每条命中是 T 号 · #轮次 · 时间 · 怎么出现的（第一次出现：之前的轮次都没有，多半是这一轮做的；"
                    "结果里也有：主人给过或之前出现过，这一轮的结果里也有；只是提到：只在主人的话里）· 片段。"
                    "主人说“之前话题给的文档/文件/结果”时先用它，链接和路径比关键词准；wiki 链接没命中时，"
                    "先查出它对应的 docx token 再搜，或读文档取标题里的词再搜。两个以上话题都像时问主人。",
     "input_schema": {"type": "object", "required": ["clues"], "properties": {
         "clues": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string"},
                   "description": "线索：飞书链接（取其中的 token）、token、文件路径、关键词"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "最多列几轮，默认 10"}}}},
    {"name": "topic_read",
     "description": "读一个话题最近几轮：主人的问题、结论（结果的开头）、检查点（阶段、已完成、产物、下一步、在等主人什么、"
                    "每一步的状态和开始/完成时间与用时、提交/部署/验收），"
                    "以及没回答的问题和选项。给 job 读指定的那一轮（如 topics_search 找到的），结果给得更长。"
                    "回答进度问题或替主人答题前用它；不要去读话题的原始记录文件。",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "topic": TOPIC,
         "job": {"type": "string", "description": "可选，轮次编号（如 2906 或 #2906）：读这一轮，turns 是到它为止的几轮"},
         "turns": {"type": "integer", "minimum": 1, "maximum": 5,
                   "description": "几轮，默认最近 2 轮；给了 job 时默认 1"}}}},
    {"name": "workspaces_list",
     "description": "列出可用的工作区：名字、路径、主人写的一行用途、最近在这里开的话题。开话题前用它选工作区。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "topic_start",
     "description": "为一件新的事开一个话题区去做（可能超过一两分钟的、要改代码或文件的、主人说开话题的），而不是在主线程里自己做；"
                    "只读、一两分钟能做完的查询和命令你自己做。话题区挂在主人这条提问下面；和已有话题区是同一件事时不要新开，用 topic_send。"
                    "一件独立的事一个话题区；"
                    "prompt 是交给话题区的任务说明：用主人的说法写清楚要做什么、范围和主人提的约束（路径、别做什么、怎么交付），"
                    "可以补指代的是哪个话题、之前定过什么（最多 200 字，只写对话里的事实）。不要写“开个话题区”“单开一个”"
                    "“放到 T 号里”这类路由的话，不要写对你说的话，不要抄附件的本机路径；不加你自己的计划和验收标准。"
                    "主人这条消息的原文和附件 Bridge 会自动附在后面。只读的、主人这条消息里亲口要做的、主人说过这类以后直接开的，"
                    "会直接开工；其余在话题区里出一张预填好的启动卡，等主人点开始。马上返回，不要等它做完：进展会出现在之后消息的快照里。"
                    + DONE,
     "input_schema": {"type": "object", "required": ["title", "prompt", "workspace", "readonly", "user_requested", "why"],
                      "properties": {
         "title": {"type": "string", "description": "话题的标题，说是哪件事：中文、中文标点（：，不用半角 : ,），20 字以内"},
         "prompt": {"type": "string", "description": TASK},
         "attachments": ATTACHMENTS,
         "workspace": {"type": "string", "description": "工作区名（workspaces_list 里的），不是任意路径"},
         "readonly": {"type": "boolean", "description": "只查看、只回答，不改任何文件、不执行有副作用的命令时为 true"},
         "user_requested": {"type": "boolean", "description": "主人在最新这条消息里亲口要求做这件事（直接下令、“去做”“开个话题”）为 true；你自己推断的为 false"},
         "why": {"type": "string", "description": "一行中文：为什么选这个工作区和配置"},
         "harness": {"type": "string", "description": "不填用设置里的默认"},
         "model": {"type": "string", "description": "不填用设置里的默认"},
         "effort": {"type": "string", "description": "思考预算，不填用设置里的默认"}}}},
    {"name": "topic_send",
     "description": "把主人的话追加给一个已有话题区（同一件事，几天前的也算）：它正在跑就作为补充，已经停了就开它的下一轮。"
                    "text 写这次交给它的内容：主人要它补做或改的事，用主人的说法，去掉“接着放到 T 号里”“转给那个话题”这类路由的话"
                    "和本机路径；主人原话和附件 Bridge 会附上。交给哪个由你判断，但要有把握（主人提了 T 号、在说刚才那件事、"
                    "只有一个话题区对得上）；两个以上都像时先用 AskUserQuestion 问主人。在回复里写明交给了哪个话题区。" + DONE,
     "input_schema": {"type": "object", "required": ["topic", "text"], "properties": {
         "topic": TOPIC,
         "text": {"type": "string", "description": "交给话题的内容：主人这次要它做或补充的事，不含路由用语和本机路径"},
         "note": {"type": "string", "description": "可选，一句背景（最多 100 字）：写在你卡上“转给”那一行，也交给话题的模型；"
                                                   "不写路径，不写给主人看的话"},
         "title": {"type": "string", "description": "可选，开下一轮时任务卡上的标题，中文，20 字以内；不填用话题原来的标题"},
         "attachments": ATTACHMENTS}}},
    {"name": "topic_fork",
     "description": "从一个已有话题分叉出新话题：新话题带着原话题的背景，原话题不受影响。相关但不是同一件事、又需要原话题的上下文时用。",
     "input_schema": {"type": "object", "required": ["topic", "title", "prompt"], "properties": {
         "topic": TOPIC, "title": {"type": "string", "description": "新话题的标题，说是哪件事：中文、中文标点，20 字以内"},
         "prompt": {"type": "string", "description": TASK},
         "attachments": ATTACHMENTS}}},
    {"name": "question_answer",
     "description": "主人说了怎么答某个话题的选择题时（如“T412 选 A”），替他答掉。只认选择题：每题都要答，选项必须是列表里的原文，"
                    "多选给列表；自由文字的回答不行，改用 topic_send 作为补充。答完告诉主人答了什么。",
     "input_schema": {"type": "object", "required": ["topic", "answers"], "properties": {
         "topic": TOPIC,
         "answers": {"type": "object", "description": "题目 id（或题目原文）→ 选项原文；多选的给数组",
                     "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}}}}},
    {"name": "topic_priority",
     "description": "主人说某个话题急时，把它在看板里本组内提到最前（不打乱“等你回答 → 进行中 → 有结果”的先后）。"
                    "主人说某个话题不用管了时，给 remove: true，把它从看板和快照里撤下；话题本身不动，它有新问题或新结果时会自己回来。",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "topic": TOPIC, "remove": {"type": "boolean", "description": "true：从看板撤下，而不是提前"}}}},
    {"name": "principle_save",
     "description": "记下主人说的一条长期适用的原则或偏好（“以后……”“原则是……”“记住……”“都要……”），之后每个话题区和你自己的每一轮"
                    "都会看到它，主人不用再重说。一次性的要求（只对这件事、这一轮）不记。scope 选 owner 是通用的、所有工作区都适用；"
                    "只对某个工作区（仓库、项目）成立的选 workspace 并写工作区名。text 用主人的说法概括成一句，最多 200 字，"
                    "不写令牌、密码这类机密。同一组里已有意思相同的会返回已有的 id，不重复记。记下后在回复里说一句“已记下：……”。",
     "input_schema": {"type": "object", "required": ["scope", "text"], "properties": {
         "scope": {"type": "string", "enum": ["owner", "workspace"],
                   "description": "owner：通用，所有话题都看到；workspace：只有这个工作区里的话题看到（你自己在这个工作区时也看到）"},
         "workspace": {"type": "string", "description": "scope 为 workspace 时必填：工作区名（workspaces_list 里的）"},
         "text": {"type": "string", "description": "原则本身，一句话，最多 200 字"}}}},
    {"name": "principle_delete",
     "description": "主人说“忘掉/删掉那条原则”“以后不用……了”时，删掉记下的那条原则。先用 principles_list 找到它的 id；"
                    "对不上是哪条时问主人。删完在回复里说删了哪条。",
     "input_schema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "原则的 id（principles_list 每行开头的，如 p1a2b3c）"}}}},
    {"name": "principles_list",
     "description": "列出记下的主人的原则：每行是 id · 通用或工作区名 · 记下的日期 · 正文。主人问记了哪些原则、要删某条、"
                    "或你要确认某条有没有记过时用。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "settings_panel",
     "description": "主人要看或改主线程的设置时，在私聊里发出设置卡；设置只能由主人在卡上改，你不能改。",
     "input_schema": {"type": "object", "properties": {}}},
)

NAMES = tuple(tool["name"] for tool in TOOLS)

# Tools only some deployments have. The MCP server lists them when its environment says so (``listed``;
# core/main_thread.py mcp_config hands the two variables on from the Bridge's own): without the variable a tool is
# not in the list, and a call to it is refused. The variable names are core/knowledge.py's ENV and HISTORY_ENV.
KNOWLEDGE_ENV = "COVEY_KNOWLEDGE"
HISTORY_ENV = "COVEY_HISTORY_CMD"
ENTRY_ID = {"type": "string", "description": "条目的 id（knowledge_search 每行开头的，如 k1a2b3c）"}
KNOWLEDGE_TOOLS = (
    {"name": "knowledge_save",
     "description": "记一条知识条目：主人说“记一下……”“记下来”时用。只记主人说的，不要自己总结着记；说“把 T483 的结论记一下”时，"
                    "先用 topic_read 读那一轮再起草。text 用主人的说法概括成一句，最多 200 字，不写令牌、密码这类机密。"
                    "scope 选 owner 是通用的；只对某个工作区成立的选 workspace 并写工作区名。同一组里已有意思相同的会返回已有的 id，"
                    "不重复记。记完在回复里列出 id 和正文。",
     "input_schema": {"type": "object", "required": ["scope", "kind", "text"], "properties": {
         "scope": {"type": "string", "enum": ["owner", "workspace"],
                   "description": "owner：通用，所有话题都带上；workspace：只有这个工作区里的话题带上"},
         "workspace": {"type": "string", "description": "scope 为 workspace 时必填：工作区名（workspaces_list 里的）"},
         "kind": {"type": "string", "enum": ["decision", "practice", "pitfall", "fact", "question"],
                  "description": "decision 决定，practice 做法，pitfall 坑，fact 事实，question 待定"},
         "text": {"type": "string", "description": "条目本身，一句话，最多 200 字"},
         "why": {"type": "string", "description": "可选：为什么，最多 200 字"},
         "source_topic": {"type": "string", "description": "可选：这条来自哪个话题（T 号，如 T483）"}}}},
    {"name": "knowledge_update",
     "description": "主人说“改成……”时，改写一条知识条目：写一条新的取代旧的（新 id），旧的变成已取代、仍可在 knowledge_search 的 history 里查到。"
                    "先用 knowledge_search 找到 id；kind 和 why 不给就沿用原来的。",
     "input_schema": {"type": "object", "required": ["id", "text", "reason"], "properties": {
         "id": ENTRY_ID, "text": {"type": "string", "description": "改写后的正文，一句话，最多 200 字"},
         "kind": {"type": "string", "enum": ["decision", "practice", "pitfall", "fact", "question"]},
         "why": {"type": "string", "description": "改写后的原因；不给就沿用"},
         "reason": {"type": "string", "description": "为什么改（主人的说法）"}}}},
    {"name": "knowledge_drop",
     "description": "主人说“删了”“作废”时，把一条知识条目作废（记录留着，不再带进任何话题）。先用 knowledge_search 找到 id。",
     "input_schema": {"type": "object", "required": ["id", "reason"], "properties": {
         "id": ENTRY_ID, "reason": {"type": "string", "description": "为什么作废（主人的说法）"}}}},
    {"name": "knowledge_search",
     "description": "搜知识条目：query 用空格隔开几个词，命中的词越多越靠前；每行是 id · 组 · 类型 · 日期 · 正文 · 来源 T 号。"
                    "查以前做过的事、之前的决定时第一步用它；没找到再 topics_search（按链接、路径、关键词找话题）。",
     "input_schema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "搜索词；留空列出最近的"},
         "workspace": {"type": "string", "description": "可选：只搜通用和这个工作区的"},
         "history": {"type": "boolean", "description": "true：也搜已被取代、已作废的"}}}},
)
HISTORY_TOOLS = (
    {"name": "history_search",
     "description": "搜 Claude、Codex 和 Bridge 的全部历史轮次（主人的一句请求加之后做的事）：问以前做过的事、之前的做法，"
                    "knowledge_search 和 topics_search 都没找到时用。query 写一句话描述；返回命中的轮次：时间、工作区、T 号、请求、"
                    "做了哪些步骤、结论开头和原文位置。最多等 70 秒；超时或没有这个命令会直接说。",
     "input_schema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "一句话描述要找的事"},
         "workspace": {"type": "string", "description": "可选：只查这个工作区"},
         "since": {"type": "string", "description": "可选：只查最近多久，如 30d（天数）"}}}},
)
ALL_NAMES = NAMES + tuple(tool["name"] for tool in KNOWLEDGE_TOOLS + HISTORY_TOOLS)


def listed(environ) -> tuple:
    """The tools the MCP server lists in ``environ``: TOOLS, with the knowledge tools when COVEY_KNOWLEDGE is
    1 and history_search when COVEY_HISTORY_CMD is an absolute path."""
    extra = KNOWLEDGE_TOOLS if str(cenv.get(KNOWLEDGE_ENV, "", environ)).strip() == "1" else ()
    if str(cenv.get(HISTORY_ENV, "", environ)).strip().startswith("/"):
        extra += HISTORY_TOOLS
    return TOOLS + extra

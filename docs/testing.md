<!-- privacy: allow-file 测试文档里讲扩展 Harness 的测试夹具与命令 -->
# 测试与验收

没有自动流水线，下面的检查都在本地手动跑。

## 本地测试

### 怎么跑

开发中只跑改动影响的测试；每个阶段结束（或合并后的集成）跑一次全量。在仓库根目录运行，`TMPDIR` 指到自己的数据目录：

```bash
python3 -B scripts/test_map.py run --why "<原因>" [路径 ...]   # 未提交改动（或给定路径）影响的模块
python3 -B scripts/test_map.py run --why "<原因>" --diff master  # 与 master 的全部差异
python3 -B scripts/test_map.py build --why "<原因>"              # 阶段末、地图过期时：全部用例含慢速档，重建地图
python3 -B scripts/test_map.py run --why "<原因>" --full         # 阶段末、地图没过期时：默认档全量
python3 -B scripts/test_map.py select [--diff REV] [路径 ...]     # 只打印要跑的 unittest 命令
python3 -B scripts/test_map.py flaky                             # 台账里失败过的测试和次数
python3 -B -m unittest discover -s tests -p test_steer.py        # 不经地图直接跑（不进台账）
```

| 项 | 规则 |
| --- | --- |
| 并行 | `run`、`build` 每个测试模块一个子进程，同时 `--jobs` 个（默认 `min(8, CPU 数)`，`--jobs 1` 逐个）；按上次耗时先起慢的。测试自己的输出不显示，失败和错误按 unittest 格式列出 |
| 超时 | 子进程崩溃、起不来或超过 `MODULE_TIMEOUT`（600 秒）算该模块一个错误，写明当时在跑的测试。超时、Ctrl-C、SIGTERM 杀掉子进程的整个进程组；模块跑完也清掉它留下的进程 |
| 地图过期 | 加了、移动或改名了模块或测试，地图就过期（`select`、`run` 会提示）。这时 `run --full` 拒绝（退出码 2，`--force` 仍跑），改用 `build` 并提交 `tests/test_map.json` |
| 台账 | 每次 `run`、`build` 追加一行 JSON（原因、范围、计数、失败的测试 ID、HEAD、内容指纹），写到 `--ledger`、`BRIDGE_TEST_LEDGER` 或 `$TMPDIR` 上一级的 `test-ledger.jsonl`。同一内容上已有覆盖本次范围的通过记录时 `run` 跳过，重查偶发失败加 `--force` |
| 临时文件 | 默认放 `/dev/shm` 下本次运行的新目录，跑完删除，每个模块一个子目录作 `TMPDIR`。仓库所有者同意内存盘只用于测试临时文件。`/dev/shm` 是 noexec：要直接执行自己写出的脚本或假 CLI，用 `support.exec_tempdir()`（建在 `BRIDGE_TEST_EXEC_TMPDIR` 下）。`--test-tmpdir DIR` 或 `BRIDGE_TEST_TMPDIR` 换基目录，`inherit` 保持 `$TMPDIR`；直接 `unittest discover` 和 MASO 解释器不切换 |

- 没改代码不重跑全量，不拿全量当修复循环。
- 新测试要在改动前的代码上失败：`python3 -B scripts/test_map.py prove --base <改动前的提交> [TEST ...]` 把新增或改过的测试方法放到那个提交的 `covey/`、`bin/` 上跑，逐条列 fail/pass（守护旧行为的测试应当 pass，在报告里写明）。
- 卡片 JSON 能否被 CardKit 接受：`scripts/cardkit_check.py --profile <机器人 profile> <样例名|文件>` 只建卡、不发消息（`--dry-run` 只量大小和节点数）。
- `tests/` 不是包，只用 `discover -s tests`；测试模块互不导入，共用的在 `tests/support/`；本地测试不调模型、不发飞书、不继承 Bridge 的环境变量。

### 按改动范围挑测试

用地图挑，不用读测试。`tests/test_map.json` 记每个测试模块跑过的函数（子进程也算）和读过的非 Python 文件。改动映射到测试的规则：

| 改动 | 选中的模块 |
| --- | --- |
| 函数体内 | 跑过这个函数的模块（嵌套函数地图不认识时按外层函数） |
| 整段新增或删除的函数、类 | 本身不选，靠改了的调用方 |
| 注释、空行、docstring | 不选（模块 docstring 按读 `__doc__` 的函数选） |
| 函数外的 import、普通赋值 | 读这个名字的函数跑过的模块，加从本文件拿这个名字的文件 |
| 类体里的赋值（dataclass、NamedTuple 字段） | 跑过这个类方法的模块，加把这个名字当属性、关键字或字符串用的函数跑过的模块 |
| 其余函数外的改动、给定的路径 | 整个文件：跑过它的模块加直接导入它的测试模块 |
| 地图不认识的 Python 文件 | 导入它的测试模块（含经 `tests/support/`）；都没有就全量 |
| Markdown | 不选；`test_structure` 每次都跑 |

导入扫描按文件缓存在台账旁的 `test-map-scan.json`。常量可能影响没跑过该文件函数的测试，所以阶段末仍要一次全量。改了共享层（worker、保存结果、会话保护、问答）时确认选中了 `test_backend_contract` 和 `test_maso_saved_delivery_text`、`test_maso_saved_delivery_file`。

测试模块按被测对象命名（`test_<模块或功能>`），不在这里列全；要知道改动该跑哪些，用 `select`。

### 不改行为的改动：副作用轨迹与补丁清单

重构、搬模块、收拢重复代码时，用 `scripts/golden_trace.py` 证明行为没变。它每个模块一个子进程，逐个测试记下：Messenger 的调用（参数和返回）、`Runtime.log` 的每一行、原子写入的文件（权限和内容）、新建的文件和目录、删除、`Popen` 的 argv、临时目录删除前 `runs/`、`queued/`、`jobs/` 下的全部文件。随机数和临时目录编号按测试 ID 固定，时间、临时路径、pid 归一化。

```bash
G=/absolute/golden/<批次>   # 不能在仓库、$HOME 或 /tmp 里
python3 -B scripts/golden_trace.py record $G/before --jobs 8 test_cards test_launch …   # 基线
python3 -B scripts/golden_trace.py record $G/before2 --jobs 8 test_cards test_launch …  # 基线再录一次，量噪声
python3 -B scripts/golden_trace.py record $G/after --jobs 8 test_cards test_launch …    # 改动后
python3 -B scripts/golden_trace.py compare $G/before $G/after --noise $G/before2        # 退出码 0：没有真实差异
```

`compare` 的差异分类：

| 类别 | 含义 | 计入 |
| --- | --- | --- |
| ORDER | 只是后台线程之间交错的先后不同，每个后台线程自己的事件和顺序一样 | 否 |
| SEQUENCE | 同一个后台线程里先后变了（比如先发飞书再存意图） | 是 |
| THREADS | 事件换了后台线程做 | 是 |
| NOISE | 两次基线之间也不同 | 否 |

- `--op` 再把 op id 归一（真实任务进程的 op id 是随机的）。测试临时文件放 `/dev/shm/golden-trace-<uid>/s<N>`（`--scratch DIR` 换位置），录完删掉。
- 临时目录名、uuid 和随机数由完整的测试 ID 定。把测试搬到另一个模块后，改动后的录制经 `child` 子命令让 `TestCase.id()` 仍报旧模块名，再把结果并回旧模块名下比较。
- 已知噪声：`test_launch` 里备份清单的哈希（`test_a_backup_keeps_each_chats_defaults`）、`test_harness_switch` 同一秒内的会话记录先后、`test_native_questions` 和 `test_detached_runs` 里两个后台线程写同一条任务记录的先后。
- `golden_trace` 不在默认测试里跑，`test_tooling_scripts` 只做冒烟检查。

搬模块或把名字移到别的模块之前，先跑 `python3 -B scripts/test_map.py patches <模块>`（`covey.app.detached`、`app.detached` 或文件路径都行）。它静态列出 `tests/` 里按这个模块的导入路径替换名字的地方（`mock.patch("模块.名字")`、`patch.object(模块, …)`、`patch.multiple`、`patch.dict`、`setattr`、写着 `"模块.名字"` 的字符串），格式是 `文件:行 方式 名字 (测试)`。运行时才拼出的目标列为 `dynamic`，要人看一眼。名字搬走后这些补丁不再生效，测试可能照样通过却不再测原来的东西，要一并改到新位置。给类的属性打的补丁跟着类走，不列出。

### 卡片 JSON 金样

`test_card_golden` 用真实的卡片构造函数画一组固定样本（任务卡、执行过程各状态及其增量写、启动卡、问题卡、代答面板、补充表单、恢复卡、回退卡、导出、换配置、本周用量、菜单面板），冻结时间（UTC+8），键排序后和 `tests/golden/cards.json`（仓库里是压缩的 `cards.json.xz`；`GOLDEN_CARDS=update` 写出 `cards.json`，用 `lzma` 压成 `.xz` 再删掉它）逐字节比较；不一致时列出新增、删除和变了的样本及第一处不同。

有意改卡片时：确认列出的样本都是这次要改的，重写文件、和改动一起提交，再做客户端验收（[卡片的客户端限制](cards.md#卡片的客户端限制)）：

```bash
GOLDEN_CARDS=update python3 -B -m unittest discover -s tests -p test_card_golden.py
git diff --stat tests/golden/cards.json
```

金样只覆盖这组样本。改到样本没画的分支（新卡片、新状态、新按钮、超预算才走的折叠和截断）时先补样本，并临时改一处文案确认测试会失败。样本只用假的 open_id、群和消息 ID，路径写 `/absolute/...`。

### 卡片示意图

README（中英文）和[使用说明](usage.md#第一次使用)里的 5 张图在 `docs/media/screenshots/`，由 `scripts/card_sketches.py` 画：用真实的卡片构造函数和一组演示数据（话题 T7「给 demo-app 加登录页」）拼出卡片 JSON，再用脚本自带的小渲染器画成 HTML，无头 Chromium 截图。不连飞书、不用任何账号，图里没有水印。卡片样式变了（`tests/golden/cards.json` 有大的 diff，或 [UI 规范](ui.md)换了样子）时重渲一次：

```bash
python3 -B scripts/card_sketches.py --out /absolute/sketches                    # 只出 HTML，先在浏览器里看
/absolute/venv/bin/python -B scripts/card_sketches.py --out /absolute/sketches --png [--browser /absolute/chrome]
python3 -B scripts/card_sketches.py --out /absolute/sketches --install          # 压成 128 色 PNG 放进 docs/media/screenshots
```

- `--png` 要装了 Playwright 的 Python；没装 Playwright 自带的浏览器时，用 `--browser` 指一个 Chromium 或 headless_shell。`--install` 要 Pillow，图压成调色板 PNG，每张 40–80 KB。
- 示意图要的字体：Noto Sans CJK（中文）、DejaVu Sans Mono（代码）。机器上没有彩色 emoji 字体，✅、⏳、⏹ 这类画成小圆标。
- 渲染器只认这几张卡用到的元素，碰到不认识的 tag 或底色直接报错，不悄悄跳过：卡片用了新元素时在 `Page.render` 里补上。颜色按 [UI 规范](ui.md#2-颜色)的浅色近似值，卡片自带的 `config.style.color`（结论带）照卡片里写的画。
- 只用演示数据：不写真名、内部名、群名、open_id 和本机路径。换数据改脚本里的 `ASK`、`TITLE`、`INPUTS` 和各 `*_sketch` 函数。
- 换了图要核对 README、`README.en.md` 和使用说明里的图注还对得上。
- README 链着的三步演示页 `docs/demo/index.html`（派到话题、回答一道题、看答案和产物）是同一批卡片和演示数据拼成的一个静态网页，页头写明是演示数据，不引用别处的脚本和字体。卡片变了和重渲示意图时一起重新生成：`python3 -B scripts/card_sketches.py --demo`（只要标准库和本仓库，不要浏览器）。

### 支持包与后端契约

`tests/support/` 不会被发现为测试。常用的：

| 文件 | 用途 |
| --- | --- |
| `world.py` | 临时状态目录里的 app；删目录前停掉 app 的后台线程，有后台线程不停就让测试失败 |
| `records.py` | 临时状态目录；`write_topic`、`write_job`、`run_files` 经生产的写函数写 topic.json、任务记录和 runs/ 文件；`saved` 读回 |
| `butler.py` | 管家测试的共同 setUp（`ButlerCase`：开关、临时状态目录、话题上下文、写 main-thread/opened 记录）；只要开关时用 `switch_on` |
| `fake_lark.py` | 记录出站发送；`FakeCardKit` 替换 Messenger 查找的 CardKit 函数，记录每次请求，可逐次指定返回或失败 |
| `fake_backends.py` | 假后端（`RecordingBackend.declare` 声明能力）、`codex_server` |
| `backend_drivers.py` | `test_backend_contract` 的后端驱动 |
| `worker_seams.py` | 去掉 worker 一轮之外的附带工作 |
| `clock.py`、`processes.py`、`git.py`、`cards.py`、`sessions.py` | 不真等的时钟；`leftover_check()`（有后台线程或子进程活过模块就失败）；git 夹具；卡片断言；直接写会话标识 |
| `launch_fixtures.py` | 启动卡和换配置共用的模型目录、会脱敏的 Harness |
| `form_transitions.py` | 测试期检查表单记录的状态变化（见下） |
| `maso_*.py` | MASO 链路、传输替身（`maso_helper`、`maso_runtime`、`web_tool_options`）、进度、并发重投用例基类 |
| `memguard.py`、`proxy.py` | 内存看门狗的 `GuardCase`，代答的 `ProxyCase` |

- `@slow`（`support/__init__.py`）只在 `BRIDGE_TESTS_SLOW=1` 时运行，`build` 总是带上。`tests/fixtures/` 是合成或脱敏的线上格式。
- `support/__init__.py` 装上 `form_transitions.install()`：covey 每写一次启动卡、换配置、回退的表单记录，都拿旧状态和新状态比对 `core/statuses.py` 的 `FORM_TRANSITIONS`，表外的变化让当时的用例失败。测试自己写的记录（调用栈里没有 covey 的帧）不查；子进程里的写入也不查。`BRIDGE_FORM_TRANSITIONS_LOG=<文件>` 把每次变化写成一行 JSON。
- 边界上的替身优先于按模块路径打补丁：任务记录写到 `records.state_dir` 再 `records.saved` 读回；CardKit 写用 `FakeCardKit`，不给 `cardkit_api` 打补丁。替身保留原函数签名，参数不对时测试失败。
- 断言整句界面文字时，从产品模块导入这句话的常量，不在测试里另抄一份；产品里还是行内字面量的，先提成模块常量。

`test_backend_contract` 对每个后端驱动（codex、claude、dsh、pi，扩展启用时再加 maso）跑同一组投递不变量：保存的回复只重投一次、不重跑模型；部分结果带说明发出、会话保持封住；部分失败不清掉更新的会话指针；失败的一轮不授权、不发正文；重启后 outbox 只发已授权的回复。`HarnessContractTests`、`SharedResultGateTests` 用假后端检查接口和结果闸门。加后端时加一个驱动。完整链用例只替换模型、进程传输、外部发送等边界，模拟通过不等于真实 SDK、进程清理或飞书发送已通过。

### 任务进程协议与跨版本检查

新旧 release 的 Bridge 和任务进程只经 `runs/<任务键>/` 和任务记录交流（AGENTS.md 规则 10，协议见[排队与任务进程](architecture.md#排队与任务进程)）。两道检查：

- `test_run_dir_golden`：调用 `core/run_dir.py`，以及 `app/runner_spawn.py`、`app/runner_ipc.py`、`app/detached.py` 经它写的 handoff.json、runner.json、wake.json、`queued/`，固定时间和随机数，把文件名、JSON 键、权限、op 格式、消息格式和两侧的回执语义（补充、回答、点击、续接请求）和 `tests/golden/run_dir.json` 比。有意改协议时用 `GOLDEN_RUN_DIR=update python3 -B -m unittest discover -s tests -p test_run_dir_golden.py` 重新生成，看 diff，再跑跨版本检查。
- `scripts/cross_release.py`：改公共核心（锁、记录与状态、调度、投递、服务管理）或 runs/ 协议之前，对现网 release 跑一次（AGENTS.md 多人部署第 5 条）。它用 git archive 导出旧提交，真起进程跑交接、唤醒、补充回执、请求与回复、Bridge 重启后接管、done 与 exit、SIGTERM、SIGKILL、部署与回滚；组合是新 Bridge + 旧任务进程、旧 Bridge + 新任务进程和两个同版本基线。环境清空，HOME 和状态目录在输出目录里，飞书调用全换成记录，非回环连接一律拒绝。新版本是本检出的工作区（含未提交的改动）。

```bash
python3 -B scripts/cross_release.py run --old <现网 release 的提交> --out /absolute/runs/cross-<提交> --jobs 6
python3 -B scripts/cross_release.py compare /absolute/runs/cross-<提交>   # 末行 all equal，退出码 0
```

- 输出目录必须给出，不能在本检出、`$HOME` 或 `/tmp` 里；非空目录只接受这个脚本先前写过的（有它的 `meta.json`）。
- `compare` 列出每个跨版本组合和同版本基线的差异，以及每次运行实际用的 release；缺结果、结果不是这次跑的、一对也没比成，都不算 all equal。时间、进程号、op id 归一化，timing 行和后台线程间先后不比。
- 有差异时先看 `base-new vs base-old`：两个 release 本来就不同的地方，跨版本组合也会出现。
- 多设备（[架构：设备](architecture.md#设备多设备默认关)）有两个只给它的场景。`devices-empty-new`：新 release 开着 `COVEY_DEVICES=1`、`devices.json` 里没有设备，把每个变体再跑一遍，要同 `base-new`、`base-old` 一样（开关开着但没有设备，行为同关着）。`device-rollback`（变体 `device_rollback`、`device_rollback_on`）：新 Bridge 建一个设备话题（`topic.json` 存 `gpu1:/home/user/proj`），旧 Bridge 和旧任务进程收到该话题的下一条消息；这一轮必须被拒绝（任务进程里没有 `backend.run`、记录是 `failed`），`compare` 打印旧 release 的说法，在本机跑了就退出 1。后一个变体是旧进程仍带着 `COVEY_DEVICES=1` 和同一份 `devices.json`。

### 静态检查

- `pyflakes covey bin tests extensions`：只允许 2 条有意的未使用导入，都在公司扩展里：它的 MASO 后端模块再导出的 `MasoSessionStore` 和 `CompletionBudget`（扩展不在时没有这两条）。
- `bash -n scripts/*.sh`；装了 ruff 时加 `ruff check --select F821`。
- `python3 -B scripts/code_map.py --check`：`AGENTS.md` 的模块表和各模块 docstring 一致。加、删、搬模块或改了 docstring 首句后跑 `--write`（`test_structure` 的 `CodeMapTest` 也查）。
- 文档引用：`test_structure.DocReferenceTest` 每次都跑，检查 `AGENTS.md`、`README.md` 和 `docs/*.md`（不含 `docs/history/`）。反引号里的仓库路径（代码地图里相对 `covey/` 的也算）和相对链接（含 `#标题`）必须存在；有意不在仓库里的路径列在 `DOC_UNTRACKED_PATHS`。代码里找不到的反引号名字只打印，外来的名字列在 `DOC_EXTERNAL_NAMES`。
- 名词：`test_glossary` 检查[名词表](glossary.md)里“不再用”的词不出现在四处：卡片金样里所有样本的文字、管家和负责人的规则全文（含唤醒说明和交给话题的说明）、两者看到的全部工具说明，以及 `covey/` 里所有字符串字面量（不含注释和 docstring）。必须保留旧词的字面量（如认旧记录的写法）列在测试的 `ALLOWED`，写明原因。
- 环境变量与选项：`test_structure.InventoryTest` 核对代码读的环境变量、命令行选项和 [configuration.md 的一览表](configuration.md#环境变量与选项一览)。

### 隐私检查

`python3 -B scripts/privacy_check.py [路径…]` 扫公开前不该出现的个人和公司信息，有命中退出码 1，输出 `文件:行: 规则`。不给路径时扫核心集合（`covey bin scripts docs tests skills README.md AGENTS.md pyproject.toml`），不扫 `extensions/`、`docs/history/`、`tests/test_map.json`。

- 通用规则在脚本里：飞书 open_id、union_id、chat_id、消息 id、App ID（`cli_` 开头）的实值形状，私网和内网 IP，真实邮箱（`example.com` 等占位除外，`git@host` 的 SSH 地址不算），令牌形状，`/mnt/bn`、`/opt/tiger`、`/home/<人名>` 这类路径。<!-- privacy: allow 规则说明里列出被查的路径名 -->
- 公司域名和内部名词的清单不进核心：`COVEY_PRIVACY_DENYLIST` 指向清单文件（每行一个词，不分大小写、按单词边界匹配；`词 | skip=tests/` 表示这些路径前缀下不查），公司扩展里放着一份 `privacy-denylist.txt`。没设置时只跑通用规则。
- 豁免：同一行写 `privacy: allow <原因>`；整个文件免查内部名词写 `privacy: allow-file <原因>`（通用规则照查）。原因必须写。
- 用法：`COVEY_PRIVACY_DENYLIST=/absolute/path/to/privacy-denylist.txt python3 -B scripts/privacy_check.py` 应当干净；`tests/test_privacy_check.py` 在清单存在时对核心集合跑一遍。

### 耗时与偶发

- 规模在 130 个测试模块上下；8 个并行时 `build` 约 45 秒（机器负载高时到 70 秒），逐个串行约 140 秒。在 NAS 上约慢 4 倍。
- 默认档跳过 `test_claude` 的慢速档。其余跳过取决于机器：要扩展的 MASO SDK、显式开启的 Claude CLI 契约、无特权 PID namespace 或开发机身份的用例。
- 子进程导入时读 NFS 上的 `.py` 可能卡在 D 状态（约 10 秒的整数倍），所以新起子进程的用例用宽的等待上限（`CHILD_DEADLINE`），模块耗时偶尔多出 10–30 秒是正常的。
- 已知偶发：`test_claude.ClaudeLiveQuestionDeliveryTests` 在同机并发跑多个全量时失败；`test_native_questions` 在并行下偶尔卡约 185 秒（NFSv4.1 委托等待）。单独重跑确认，`flaky` 看历史。

### GitHub 上的 CI

`.github/workflows/ci.yml` 在推 `main` 和 PR 时用 Python 3.10、3.11、3.12 各跑一遍静态检查、隐私检查、文档链接和 `run --full`。

- 时区设成 `Asia/Shanghai`，和跑 Covey 的机器一样；测试里写死的日期、时刻按 UTC+8 写。新测试仍别依赖机器时区：代码按固定 +08:00 算的（如早晚汇总的时刻）就用同一个时区算期望值，按本机时间算的就在测试里固定 `TZ`。
- 测试里的时间串按线上写法 `+08:00` 带冒号；`%z` 写出的 `+0800` 在 3.10 的 `fromisoformat` 上读不出。
- CI 不装公司扩展，整模块 `require_extension()` 的测试在那里收不到用例；`test_map.py` 比对用例清单时不算这些模块，开着扩展 `build` 出的地图在 CI 上也不算过期。
- CI 除了 pyflakes 什么包都不装（核心不依赖第三方包）。测试和核心代码在模块加载时只导入标准库和本仓库的模块；可选的包（Pillow、lark-oapi、Playwright）放进 `try … except ImportError` 或用到它的函数里，要它的用例用 `unittest.skipUnless` 跳过，这样没装它的机器上用例数不变。`test_structure` 的 `StandardLibraryTest` 查这一条。本机想看 CI 收到的用例，用 `COVEY_EXTENSIONS='' python3 -S`（不加载第三方包）跑。
- 某个测试模块在这台机器上导入不了时，`run` 先指出它和导入错误，再照常跑（它算一个错误），不说地图过期；`build` 拒绝写地图。
- 全量有失败时，`scripts/ci_retry_modules.py` 从台账（`/var/lib/covey-ci/`，`test_map.py` 不在 `$HOME` 和临时目录下记台账，GitHub 的 `runner.temp` 在 `$HOME` 下）最后一行取失败的模块（最多 4 个，台账要列全每个失败），再跑一次，第二次还失败才算 CI 失败；第一次的失败留在日志里，并有一条 warning。
- 连着推时，后一次推送取消前一次还没跑完的运行（`concurrency`），被取消的运行不发失败邮件。

## 真实环境验证

- Claude CLI 契约：`CLAUDE_BRIDGE_REAL_CLI=1 python3 -B -m unittest discover -s tests -p test_claude.py -k InstalledClaudeContractTests`，本机 `claude` 连本地模拟接口，配置隔离。升级 Claude Code 后跑一次。
- 扩展的 MASO `--check-runtime`：解释器、固定 revision、关键接口；不加载模型。
- 部署或重启后在 Bridge 的实际环境里发一次无副作用的真实模型请求（[健康诊断](troubleshooting.md#健康诊断)），终端里成功不能代替。

## 飞书客户端验收

卡片表单、按钮回调和渲染只有真实客户端能证明（[卡片的客户端限制](cards.md#卡片的客户端限制)）。改了卡片结构、表单、按钮或回调就要做：

1. 在专门的验收群里开新话题（不在机器人的主私聊里）、选小工作区，由本人或授权的自动化操作。确认部署已生效（`covey_service.py status` 的进程身份已换）再开新任务，旧任务进程按旧代码响应（[部署新版本](deploy.md#部署新版本)）。
2. 卡片文字、按钮名和提示和代码里的字符串逐字一致。
3. 每次点击在 `bridge.log`（转发的还有 `runs/<任务键>/runner.log`）里有一行 `card action kind=<按钮类型> message=<消息 ID 末 10 位> event=<事件 ID 末 10 位> outcome=<结果> elapsed_ms=<耗时>`。提交回答（原任务已结束）后还有一行 `card follow-up kind=bridge_question …`。行数少于点击数：点击没到（[排障](troubleshooting.md)）；outcome 不是 `handled`、`forwarded`、`workspace_manager`、`launch_option`、`directory`：被拒绝，按 outcome 查（[事件、执行过程与卡片](cards.md)），旧卡上预期 `retired` 或 `retired_harness`。
4. 对照记录：任务的 `status`、`execution_status`、`delivery_ok`，问题的 `status`、`delivery_state`，补充的 `status`。

场景：快速双击只入队一次、换配置或选目录后开始、续接、补充回执只前进、多题表单、重投不重跑模型、停止、翻页、机器人重启后卡片接着刷新（日志 `adopted task runner`）；每个 Harness 在独立工作区跑同一组，覆盖运行中的阻塞提问。计时和任务记录的 `timings`、`latency_ms` 对齐（[执行指标口径](architecture.md#执行指标口径)），只报观察到的区间。

### 飞书网页端的验收工具

在网页飞书上看卡片、点按钮的验收工具，用主人在代答里登录过的网页端 profile（`<state>/proxy/web`，只由下面的浏览器服务打开，别的工具要用先复制一份）；代答发送本身不经过它。

### 网页端

代答发送不经网页端。`feishu/web_client.py` 只剩验收页面要用的：打开会话列表（`open_messenger`，设备未授权时报 `UNAUTHORIZED`）、按名称找会话（`find_chat`、`openers`，至多 `CANDIDATES` 个同名的）、`header`、`clear`、`reset`、`launch`。`core/proxy.py` 的 `LAUNCH_FAILED` 等旧原因文字仍留着，给旧批次用；`settings.json` 的 `web_python`、`web_home` 照样能读。

### 浏览器服务

`feishu/web_service.py`：常驻进程（`bin/covey_feishu_web.py serve --home <state>/proxy/web`，用装了 Playwright 的解释器起），是网页端登录 profile 唯一的打开者，只给验收页面用。Bridge 不起、不认领它；验收客户端用 `--start` 起。

- 单实例锁 `service.lock`，本机 socket `web.sock`（0600，目录 0700），日志 `service.log`。连续 `IDLE_SECONDS`（30 分钟，`--idle` 可改）没有命令就退出。
- 每条命令一行 JSON，回答最后一行 `"done": true`：`ping`（随时立即回）、`stop` 和 `accept_*`；其余命令按到达顺序一条条跑。旧的 `send`、`login`、`status` 一律 `refused`。
- **停靠**（`park`，`accept_close` 时）：换成新标签页加载会话列表，免得打开的会话把对方消息标成已读；列表 `STUCK_SECONDS` 不出来就 `reset` 再试，还不行停到 `PARK`。`accept_open` 遇到还在加载的停靠页先等（`WARM_SECONDS`）。
- **验收页面**：`bin/covey_feishu_web.py accept open|post|text|click|fill|scroll|shot|close --home <state>/proxy/web [--title …] [--text …] [--at 名字] [--dy 像素] [--out PNG] [--start PYTHON]`（或 `web_service.accept`），一行 JSON 结果。`click`、`fill` 作用在最后 `NEWEST_ROWS` 行里最新的那张卡，主列表没有时用右侧打开的话题面板。`open`、`post`、`click`、`fill`、`scroll` 先核对会话标题在 `accept_chats()` 里（`COVEY_ACCEPT_CHATS` 逗号分隔的会话标题），不在就拒绝；没设这个变量时一律拒绝。只把专门用来验收的群和测试机器人的私聊写进去：打开会话会把消息标成主人已读。

## 发布记录要求

- **实跑结果**：命令、项数、跳过及原因、失败项；全部实跑通过才能写全部通过。
- **基线失败**：如实列出，写明改动前的主干是否同样失败、和改动是否有关。
- **分类**：本地模拟、真实接口、真实客户端分开写。
- **客户端待验**：改了卡片、表单、按钮或回调而没在真实客户端点过的，标“客户端待验”并写场景。
- **部署证据**：部署前 `restart_blockers` 为空，Bridge 进程身份变了，仍在运行的任务进程和 release（`runs/*/runner.json`），Session 文件和原生会话目录不变。
- **独立验收**：整机重启、真实八小时长任务、各模型账号的可用性单独验收；没做就写没做。

部署记录和验收数据写在自己的数据根，不进仓库。

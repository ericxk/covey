# 参与开发 Covey

欢迎提 Issue 和 Pull Request。下面是改代码前要知道的事。

## 开发环境

只需要 Python 3.10+，核心不依赖第三方包（飞书 SDK 是可选的，见 `pyproject.toml`）。检查工具用 `pip install pyflakes`。

## 先读哪里

- [AGENTS.md](AGENTS.md)：代码地图和“最容易改坏的规则”，改动前先读相关几条。
- [架构与开发说明](docs/architecture.md)：分层、任务生命周期、锁、状态和投递。
- [测试与验收](docs/testing.md)：怎么挑测试。

## 测试

开发中只跑改动影响的测试，阶段结束跑一次全量：

```bash
python3 -B scripts/test_map.py run --why "改了什么" [改动的路径…]   # 按改动选测试模块
python3 -B scripts/test_map.py run --why "阶段收尾" --full          # 全量
```

- 加了、移动或改名了测试，测试地图就过期：运行 `python3 -B scripts/test_map.py build --why "..."`（需要 Python 3.12）并把 `tests/test_map.json` 一起提交。
- 新测试应当在改动前的代码上失败：`scripts/test_map.py prove --base <提交>`。
- 测试不调模型、不发飞书、不需要 `lark-cli`。临时文件放 `/dev/shm`（没有就用 `--test-tmpdir DIR`）。
- 改了卡片或交互：单元测试证明不了飞书客户端里的点击，要在真实客户端里验收；卡片 JSON 变了，`tests/golden/cards.json` 会有 diff，按[测试与验收](docs/testing.md)处理。

## 静态检查和隐私检查

```bash
python3 -m pyflakes covey bin tests scripts
bash -n scripts/*.sh
python3 scripts/privacy_check.py
```

`scripts/privacy_check.py` 扫描要公开的文件里的个人和公司信息（飞书 id、内网 IP、真实邮箱、令牌形状、个人路径）。示例和测试里的 id、路径、邮箱请用一看就是假的值（`ou_fake`、`/absolute/...`、`user@example.com`）。部署自己的内部名词清单放在仓库外，用环境变量 `COVEY_PRIVACY_DENYLIST` 指向它。

## 扩展

只有部分部署需要的功能做成扩展，放 `extensions/<包名>/`，不改核心：核心不按名字引用扩展，扩展通过 `manifest.py` 把数据并进核心的表。没配置时不起线程、不加入口、不改已有行为。规则见 [架构](docs/architecture.md#扩展)。需要扩展的测试用 `tests/support` 里的 `requires_extension`，扩展没启用时跳过。

## 约定

- 凭据、令牌、账号、open_id、群 ID、主机名不进仓库、文档、测试和提交说明。个人环境靠配置文件或 `COVEY_*` 环境变量传入。
- 新读环境变量或加命令行参数，要在 `docs/configuration.md` 的“环境变量与选项一览”加一行。
- 改公共核心（锁、记录与状态、调度、投递、服务管理）时，开关关着的行为要和原来一样，并跑全量。
- 提交信息用英文，写清改了什么、为什么。

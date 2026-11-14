## 改了什么 / What changed

<!-- 一两句话：改了什么，为什么。 -->

## 怎么测的 / How it was tested

- [ ] `python3 -B scripts/test_map.py run --why "…"`（改动影响的测试）
- [ ] 改了公共核心（锁、记录与状态、调度、投递、服务管理）：`run --full` 通过，开关关着的行为不变
- [ ] 加了、移动或改名了测试：`test_map.py build` 并提交 `tests/test_map.json`
- [ ] `python3 -m pyflakes covey bin tests scripts` 和 `bash -n scripts/*.sh`
- [ ] `python3 scripts/privacy_check.py` 和 `python3 scripts/check_doc_links.py`
- [ ] 新读环境变量或命令行参数：已在 `docs/configuration.md` 加一行
- [ ] 改了卡片或交互：附真实客户端截图（去掉个人信息）；`tests/golden/cards.json` 有 diff 时说明原因

## 注意 / Notes

- 没有令牌、账号、open_id、群 ID、主机名或真实聊天内容（测试里用 `ou_fake`、`/absolute/...`、`user@example.com`）。
- 提交信息用英文。

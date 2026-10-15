# F26 验收与测试

没有 key 就记 blocked。脚本模型不代替这次 live 验收。`live_ready` 保持 false。F26 在 `STATUS.json` 和 `tasks/backlog.yaml` 里是 `blocked`，不是 `done`。`blocked` 不是通过。

要跑真实探针，三个变量都要有：

- `AETHER_LIVE_MODEL_KEY`
- `AETHER_LIVE_MODEL_BASE`
- `AETHER_LIVE_MODEL_ALIAS`

| 验收 | 测试 |
|---|---|
| 缺这三个变量时结果是 blocked，研究和编码都是 not_run | `tests/unit/test_live_harness.py::test_missing_live_key_is_blocked_and_not_a_pass` |

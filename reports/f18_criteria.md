# F18 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。通知只写在站内表里。

| 验收 | 测试 |
|---|---|
| owner 批准来源、频率和预算之后才启用，并创建 Temporal schedule | `tests/integration/test_watches.py::test_watch_requires_approval_skips_unchanged_and_does_not_send_out` |
| 水位没变就不总结 | `tests/integration/test_watches.py::test_watch_requires_approval_skips_unchanged_and_does_not_send_out` |
| 同一个 trigger 不产生第二条通知 | `tests/integration/test_watches.py::test_watch_requires_approval_skips_unchanged_and_does_not_send_out` |
| 停止 watch 之后不再派发 | `tests/integration/test_watches.py::test_watch_requires_approval_skips_unchanged_and_does_not_send_out` |
| 默认不发送外部消息 | `tests/integration/test_watches.py::test_watch_requires_approval_skips_unchanged_and_does_not_send_out` |

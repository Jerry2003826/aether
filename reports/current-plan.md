# F18 主题跟踪

目标：owner 批准来源、频率和预算之后才启用 Temporal schedule。无变更不总结。重复 trigger 不重复通知。停止后不再派发。默认不发外部消息。

合同：`docs/05_knowledge.md`，`docs/14_BOUNDARIES_AND_FAILURES.md`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_watches.py`

进度：本地 1 passed。还没标 done。

阻塞：无。

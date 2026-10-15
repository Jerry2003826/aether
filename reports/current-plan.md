# F08 最小真实 UI 与可回放事件

目标：任务提交、进度、审批、证据和费用接到真实 API。SSE 断线后按 sequence 补齐。审批在服务端鉴权。

合同：`docs/08_api_ui.md`。

拟改路径：`services/api/aether_api/app.py`、`apps/web`、`tests/integration`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration`

进度：F07 dispatcher 已用真实 Temporal 投递 outbox。F08 未开始。

阻塞：无。F01 仍 blocked。

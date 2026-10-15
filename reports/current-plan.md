# F04 模型与工具准入网关

目标：ModelGateway 和 ToolRegistry。job token 不能被请求体改写 project/task。provider key 不进入 actor、UI 或日志。用本地 fake upstream 测真实 HTTP，而不是返回固定成功。

合同：`contracts/interfaces.py` 的 ModelGateway；`config/tools.example.yaml`。

拟改路径：`services/gateway/aether_gateway/`、`tests/integration/test_gateway.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_gateway.py`

进度：未开始。

结果：无。

阻塞：无。live 模型仍 not_run。

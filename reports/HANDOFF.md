# 交接

PR #1 已可审查。提交 `767b212` 上 GitHub Actions 与 SonarCloud 为成功。其后又完成 F03–F06。

## 最近测试

- F03 全量：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration` → 32 passed（在 F04 之前）。
- F04：`uv run --locked --no-build pytest -q tests/integration/test_gateway.py` → 1 passed。
- F05：`uv run --locked --no-build pytest -q tests/integration/test_budget.py` → 1 passed。
- F06：`uv run --locked --no-build pytest -q tests/integration/test_operations.py` → 1 passed。

## 状态

- F00、F02、F03、F04、F05、F06：done
- F01：blocked。BGE-M3 权重未加载，`dimension_probed` 为 null。OpenHands 导入记为 partial，没有调用模型。
- F07 起未开始。

live 模型、CodeGraph、Hermes 仍是 not_run。

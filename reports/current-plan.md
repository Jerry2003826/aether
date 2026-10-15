# F05 事务预算、用量与 unknown 账单

目标：预算预留、结算和对账。并发 reservation 原子限额，重复结算不重复扣款。超时可能计费保持 held。unknown 价格或 usage 不算 0。

合同：`budget_accounts`、`model_requests`。

拟改路径：`packages/storage/aether_storage/budget.py`、`tests/integration/test_budget.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_budget.py`

进度：未开始。

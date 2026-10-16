# P2-09 复核出口、所有者批准、活动时间与工作流投递

任务：从 `6f223f7` 做本路六项。ADR-F34 已被 PR #24 占用，决定记为 ADR-F35。NEEDS_REVIEW 有所有者出口；worker 不以所有者名义自动批准；active elapsed 不计排队、审批、暂停和复核；工作流可结束并 continue-as-new，ID 复用为 REJECT_DUPLICATE，投递有租约；审批失败不把幂等键留在 0；超额可对账；已成功任务的答案和验收报告不能删除。

相关合同：不改 `contracts/`、`reference/` 或验收标准。不把 `live_ready` 改成 passed。不新增 OpenAPI 操作。没有预批准模板登记，因此不实现低风险自动批准。

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/integration
```

进度：产品路径和本路测试已接上。先失败的用例是 `test_worker_does_not_approve_the_contract_as_the_owner`：没有批准时不得写入 `goal_contracts.approved_by`。

结果：冻结检查通过。参考套件 76。单元 231 passed。集成 320 passed、38 failed、1 warning。38 条失败都是本机没有 Docker、Playwright Chromium 和 Hermes，没有 skip。本路定向 16 passed，worker 进程、p0 批准和历史回放 13 passed。

阻塞：本机缺 Docker、浏览器和 Hermes，交给 CI。不改 `live_ready`。已被终止且任务尚未终态的 workflow 不会重新启动。

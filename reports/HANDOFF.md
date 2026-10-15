# 交接

F04–F06 在全量套件通过后保持 done。四条 Critical（请求哈希、token 预留、连接重置、资源当前版本）的复现测试在 `tests/integration/test_budget.py` 与 `tests/integration/test_operations.py`。对照表仍是 `reports/f04_f06_criteria.md`。

## 全量测试（标 done 之前）

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
...................................................................      [100%]
67 passed, 2 warnings in 12.57s
EXIT:0
```

警告是既有的 Starlette `httpx` 弃用提示，以及 Pydantic 对 `ChatCompletionReasoningItem` 的 `ReadOnly` 提示。没有跳过的集成用例。

## P1 之后的全量测试

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
........................................................................ [100%]
72 passed, 2 warnings in 15.23s
EXIT:0
```

P1 覆盖：

- `aether_api` 不能写 operations、budget_accounts、model_requests、audit_events、verification_results、evaluation_runs；不能 UPDATE/DELETE command_outbox。新表不继承 DML。
- SUCCEEDED、FAILED、CANCELLED 不能改回 QUEUED、DRAFT、RUNNING。API 仍不能把任务推进到 DRAFT/QUEUED 以外。
- `ensure_database` 写入 `alembic_version=0004`。删掉版本行后再 `alembic upgrade head` 会在已存在的 `owners` 上失败。
- `AETHER_ALLOW_NON_LOOPBACK=1` 单独不能绑定 `0.0.0.0`，还要 Secure cookie 和 TLS 证书文件。
- 登录失败名额在密码校验前原子占用。8 个并发错误口令只有 5 个进入校验，其余 429。

## 状态

- F00、F02、F03、F04、F05、F06、F07：done
- F01：blocked。BGE 权重未加载，`dimension_probed` 为 null。
- F07 的 Workflow、Activity 和 outbox 事务是真实的。命令发件箱还没有 dispatcher 把行投递到 Temporal。下一步做这件事，不把未投递标成已投递。
- F08 及之后未开始。F15 不跳过。

live 模型、CodeGraph、Hermes 仍是 not_run。

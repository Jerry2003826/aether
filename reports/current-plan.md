# P1 权限与登录边界

目标：收紧 `aether_api` 对 operations、budget 等权威表的 DML；终态任务不能回到可调度状态；`ensure_database` 盖章 `alembic_version`；非 loopback 没有 TLS 和 Secure cookie 时拒绝绑定；登录失败计数在并发下原子。

合同：`docs/06_security.md`，`contracts/schema.sql` 关于 API 不能写验证证据、晋升分数和最终任务状态。

拟改路径：`migrations/sql/0003_grants.sql`、`migrations/sql/task_status_guard.sql`、`packages/storage/aether_storage/bootstrap.py`、`services/api/aether_api/settings.py`、`services/api/aether_api/app.py`、`services/api/aether_api/throttle.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration`

进度：代码和全量测试已完成。下一步是把 outbox 里的命令经真实 Temporal dispatcher 投递，不把 F07 标成已有投递。

阻塞：无。F01 仍因 BGE 维度未实测而 blocked。

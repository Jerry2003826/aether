# F02 领域迁移、单 owner 认证与项目隔离

目标：把 `contracts/schema.sql` 变成可重复的迁移，用运行时数据库角色验证 RLS 和项目隔离，并提供 bootstrap/session API。

合同：`contracts/schema.sql`、`contracts/openapi.yaml` 的 auth/project/health。

拟改路径：`migrations/`、`packages/storage/aether_storage/`、`services/api/aether_api/`、`tests/integration/`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_auth.py tests/integration/test_rls.py`

进度：未开始。

结果：无。

阻塞：无。F01 的权重缺口不阻塞本任务。

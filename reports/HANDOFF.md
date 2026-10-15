# 交接

F00 已完成。F01 仍因 BGE 权重未加载而 blocked。F02 已用真实 PostgreSQL 通过。

## F02 测试

命令：

```bash
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run pytest -q tests/unit tests/integration
```

结果：`19 passed`。覆盖 Alembic 升级、保留数据的加列、回滚后再升级；非 superuser 的 RLS、复合外键、API 不能把任务标成 SUCCEEDED、也不能写 verification_results；CSRF、Origin、会话撤销、空闲和绝对过期、登录限流、回环引导；两个项目的列表和详情不会串库，事务结束后连接不再保留 project scope。

本机数据库是 PostgreSQL 16.15。CI 镜像仍是 `pgvector/pgvector:0.8.7-pg17`，本机没有拉取。

## 未运行

- GitHub Actions 还没有在远端完成。
- F01 权重、live 模型、Docker、CodeGraph、Hermes。

## 下一步

实现 F03 产物存储和安全导入。

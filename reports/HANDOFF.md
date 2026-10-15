# 交接

当前提交完成 G0 的 F00，并把 F01 标为 blocked。产品内核尚未开始。

## 已执行

- `uv lock && uv sync`：CPython 3.13.16，控制面锁在 `uv.lock`。
- `pnpm install && pnpm typecheck`（Node 24.22.0）：`apps/web/pnpm-lock.yaml`，类型检查通过。
- `uv run python scripts/probe_f01.py`：OpenHands 1.54.0 四个包可导入，`LLM.base_url` 可指向本地网关；没有调用模型。BGE 权重未下载，`dimension_probed` 为 null，任务 blocked。
- 本机 PostgreSQL 16.15 + pgvector 0.6.0 可连接。CI 镜像是另外探测到的 `pgvector/pgvector:0.8.7-pg17`，本机没有拉取该镜像。
- Temporal CLI 1.9.1 的 `server start-dev` 健康检查通过。这是开发服务，不是发布用的持久化。
- `uv run pytest -q tests/unit tests/integration` 在设置 `AETHER_REQUIRE_SERVICES=1` 后通过。
- `uv run python scripts/verify_freeze.py` 通过。

## 未运行

- GitHub Actions 尚未在远端执行。
- FlagEmbedding 安装与 BGE-M3 权重加载：not_run / blocked。
- live 模型、Docker、CodeGraph、Hermes：not_run。

## 下一步

实现 F02：Alembic 迁移、单 owner 认证、非 superuser 的 RLS 与项目隔离测试。

# R8-B 开源库替换

任务：变基到 `ab1ebef` 之后，把编码执行、技能用例调度、API 请求体、非密钥配置、密钥扫描和容器/进程检查换成固定版本的开源库。沙箱策略、凭证分离、预算账本、ArtifactStore、SSRF、审批和 CodeGraph adapter 保持手写。采集解析已是 ADR-F41，威胁情报浏览器已是 ADR-F43，本决定记为 ADR-F42。不改 `contracts/`、`reference/` 和 `readiness.live_ready`。

拟改路径：

- `services/worker/aether_worker/coding_sandbox.py`、`openhands_sandbox_entry.py`、`coding_protocol.py`、`openhands_image.py`、`container_runtime.py`
- `services/worker/aether_worker/skill_execute.py`、`secret_scan.py`、`runtime_config.py`、`role_launch.py`、`runner.py`
- `services/api/aether_api/body_models.py`、`settings.py` 以及调用这些模型的路由
- `infra/openhands-sandbox.Dockerfile`、`.github/workflows/ci.yml`、`.gitleaks.toml`
- `docs/DECISIONS.md`、`docs/04_execution.md`、`docs/08_api_ui.md`、`docs/10_operations.md`、`README.md`、`MASTER_SPEC.md`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
gitleaks detect --source . --config .gitleaks.toml --redact --no-banner
```

进度：七项替换都接到库调用。`JOB_IMAGE` 常量未改。`f27.codegraph_enabled` 改为 true。

结果：2026-10-16 变基到 `13bc096` 之后，冻结检查通过（`api_operations` 50），参考套件 76 OK（1.259s），单元 313 passed、1 warning（24.65s）。Gitleaks 8.30.1 无泄漏。本机没有 Docker、Postgres 和 Temporal，集成测试、web typecheck 和 doctor 没有跑。`live_ready` 保持 pending。

修正：OpenHands 安装改为 `uv sync --locked --no-build`。`func-timeout==4.3.5` 使用仓库内 wheel。`exec`、`kill`、日志尾部和 runner 构建镜像已接上。入口只加载 `LocalWorkspace.execute_command` 用到的模块，避开会超过 256 MiB 的 LLM 栈。一次 `/proc` 采样的父进程峰值是 41736 KiB。CI 结果还没有记入。

阻塞：本机没有 Docker，编码作业的容器终态仍要由 CI 的 integration 证明。

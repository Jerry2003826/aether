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

结果：这一笔变基提交还没有重跑测试。上一笔在 `a2b9313` 上的结果是冻结检查通过（`api_operations` 50），参考套件 76 OK，单元 290 passed、1 warning（21.43s）。`live_ready` 保持 pending。

阻塞：编码镜像要在 CI 的 Docker 上构建后，集成测试才能看到 OpenHands 容器终态。

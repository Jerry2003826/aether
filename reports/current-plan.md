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

进度：编码作业由 OpenHands Agent 生成补丁。模型请求经 `ModelGateway.relay_chat`，按 token 结算。模型套接字是属主权限加 uid 65534 的 POSIX ACL。编码容器的工作目录不再是 `/tmp`。沙箱策略仍手写，内存和 swap 上限是 805306368。`JOB_IMAGE` 常量未改。`f27.codegraph_enabled` 仍是 true。`live_ready` 仍是 pending。

结果：2026-10-16 冻结检查通过（`api_operations` 50），参考套件 76 OK（1.308s），单元 318 passed、1 warning（36.55s）。Gitleaks 8.30.1 无泄漏。入口采样父进程 283888 KiB，进程树 306484 KiB。本机没有 Docker、Postgres 和 Temporal，集成测试、web typecheck 和 doctor 没有跑。`origin/main` 仍是 `13bc096`。

修正：入口不再挡住 LLM 栈。`file_editor` 和 `terminal` 由 Agent 调用。容器只挂一个模型 Unix socket。没有用量时 `mark_unknown`，不再按 0 结算。套接字不再对 other 开放。桥接半关闭不再从 `finally` 返回。

阻塞：本机没有 Docker。`test_openhands_agent_edits_through_the_gateway_and_settles_tokens` 要由 CI 的 integration 证明；缺 Docker 时该测试失败，不跳过。

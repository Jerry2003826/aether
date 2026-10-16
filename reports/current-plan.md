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

进度：编码作业由 OpenHands Agent 生成补丁。`relay_chat` 用编码作业的 fencing 和 operation 租约做一次一票，不改 `tasks.status`。聊天补全忽略请求体 timeout，只转发白名单字段。协议判定前用基线测试覆盖副本并核对哈希。模型套接字是属主权限加 uid 65534 的 POSIX ACL。编码容器的工作目录不再是 `/tmp`。沙箱策略仍手写，内存和 swap 上限是 805306368。`JOB_IMAGE` 常量未改。`f27.codegraph_enabled` 仍是 true。`live_ready` 仍是 pending。

结果：变基到 `origin/main` 的 `ab1ebef`。ADR-F41 与 ADR-F43 保留，ADR-F42 仍是编码中继。`playwright` 钉在 `==1.64.0`，控制面仍有 pydantic-evals、pydantic-settings、detect-secrets、docker 与 psutil。编码中继在有 `AETHER_LIVE_MODEL_ALIAS` 时把上游模型换成该别名，并同时计量 JSON 与 SSE usage。单轮预留默认 4096 token，可按次追加到任务非验收上限；实测量超过预留时按实际 token 结清并释放冻结。Agent 未正常结束或被预算拒绝时作业是 FAILED。2026-10-16 本地冻结检查通过（`api_operations` 50），参考套件 76 OK（1.290s），单元 361 passed、1 warning（42.67s）。本机没有 Docker、Postgres 和 Temporal。这次预留修正的 CI 还没有记入。没有合并。`live_ready` 仍是 pending。

修正：入口不再挡住 LLM 栈。`file_editor` 和 `terminal` 由 Agent 调用。容器只挂一个模型 Unix socket。没有用量时 `mark_unknown`，不再按 0 结算。套接字不再对 other 开放。桥接半关闭不再从 `finally` 返回。

阻塞：本机没有 Docker。`test_openhands_agent_edits_through_the_gateway_and_settles_tokens` 要由 CI 的 integration 证明；缺 Docker 时该测试失败，不跳过。

# R7 凭证分离、镜像摘要、产物恢复与崩溃演练

任务：从 `cc471e3` 做 P2-09 里本路的五项，变基到 `b75fb36`。ADR-F34 至 ADR-F38 已由采集、任务流程和编码作业占用，ADR-F39 已由采集域名、抓取终态和跟踪批准占用，本决定记为 ADR-F40。不改 `contracts/`、`reference/`、验收条件和 `readiness.live_ready`。编码作业、技能评测和 ADR-F39 的采集边界保留 main 已合入的行为。`JOB_IMAGE` 使用同一 digest。

拟改路径：

- `services/worker/aether_worker/role_launch.py`、`role_http.py`、`role_client.py`、`verifier_process.py`、`runner_process.py`、`loopback.py`、`sandbox_delegate.py`
- `services/gateway/aether_gateway/__main__.py`
- `services/worker/aether_worker/service.py`、`goal_effect.py`、`runner.py`、`verification.py`、`sandbox_image.py`、`startup.py`、`coding_sandbox.py`、`skill_execute.py`
- `packages/storage/aether_storage/artifacts.py`
- `services/worker/aether_worker/crash_drill.py`、`scripts/run_crash_recovery_drill.py`
- `docs/DECISIONS.md`、`docs/10_operations.md`、`README.md`、`infra/images.lock.json` 与由此重建的 `MASTER_SPEC.md`
- `tests/unit/test_r7_*.py`、`tests/integration/test_r7_*.py`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q tests/integration
(cd apps/web && pnpm typecheck)
```

进度：变基到 `b75fb36` 后保留 ADR-F36 至 ADR-F39，本路改为 ADR-F40。`service.py` 同时保留 `dispatch_approved_tools`、`execute_queued_coding_jobs`、`execute_queued_ingestions` 和 `launch_privileged_roles`。`goal_effect.py` 同时保留所有者批准与凭证拆分。演练容器设置 `USER=temporal` 与 `HOME=/state`。回环请求不跟随代理。角色监听用 `handle_request` 轮询。worker 进程里的编码沙箱改由 runner 执行 docker，技能评分改由验收进程执行。

结果：变基到 `b75fb36` 之后，2026-10-16 本地冻结检查通过，参考套件 76 OK，单元 277 passed（105.49s），集成 382 passed、1 warning（547.92s）。web typecheck 在 Node 22.14.0 上退出码 0。doctor `--require-services` 退出码 0，`reports/doctor.json` 没有提交。`live_ready` 保持 pending。这次提交的 CI 还没有记入。

阻塞：同一用户仍能打开 Docker socket；网关进程持有 worker 库口令；子进程不自动拉起；演练不碰共享内存 Temporal。

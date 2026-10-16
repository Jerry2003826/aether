# SYS-06 记忆来源与 SYS-04 验收条款

任务：外部审计基线 `8b90363`，变基到 `85d27d3`。只修 SYS-06 与 SYS-04。不改 SYS-01、SYS-02、SYS-03、SYS-07、SYS-08 的文件，不改 `contracts/`、`reference/`、`STATUS.json`。

相关合同：记忆认识状态、产物哈希、`verification_results`、`goal_contracts`。不改合同文件。

拟改路径：

- `services/worker/aether_worker/memory.py`
- `services/worker/aether_worker/goal_effect.py`
- `services/api/aether_api/memory_routes.py`
- `tests/integration/test_memory.py`
- `tests/integration/test_r6_memory_contract.py`
- `tests/unit/test_r6_acceptance.py`
- `docs/DECISIONS.md`（ADR-F31；ADR-F30 已由 SYS-02/07 使用）

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration
uv run --locked --no-build python scripts/doctor.py --require-services
```

进度：先写会失败的集成用例，再改确认继承、降级授权和测试运行账本，以及草案条款冻结。变基时 ADR-F30 冲突，本决定改为 ADR-F31，并保留 SYS-02/07 的决定与交接。

结果：确认不降敏、测试运行要验证者账本、草案条款被冻结。正向 `observed_test` 由 `test_test_run_requires_verifier_ledger_bound_to_the_bytes` 覆盖。全量结果见 `reports/HANDOFF.md`。

阻塞：无。

决策：ADR-F31。

# SYS-02 协议评分与 SYS-07 指标校验

任务：关闭复审 `8b90363` 的 SYS-02 与 SYS-07。不改 SYS-01/03/04/06/08 所属文件。该工作已由 PR #21 合入 `85d27d3`。

相关合同：不改 `contracts/`、`reference/`、验收标准或 `STATUS.json`。

拟改路径：

- `services/worker/aether_worker/verification.py`
- `services/worker/aether_worker/grader_protocol.py`
- `services/worker/aether_worker/grader_runner.py`
- `services/worker/aether_worker/skill_eval.py`
- `tests/unit/test_skill_judge_metrics.py`
- `tests/unit/test_grader_protocol.py`
- `tests/integration/test_sys02_isolated_grader.py`
- `docs/06_security.md`、`docs/DECISIONS.md`（ADR-F30）与由此重建的 `MASTER_SPEC.md`

测试命令：

```text
uv run --locked --no-build pytest -q tests/unit/test_skill_judge_metrics.py tests/unit/test_grader_protocol.py tests/integration/test_sys02_isolated_grader.py
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration
```

进度：旧代码上 `"NaN"` 等指标得到 ELIGIBLE（22 failed / 20 passed），错误实现替换 pytest `Function.runtest` 后评分 PASS。协议断言已留在父进程；白盒 pytest 不再认证或自动发布；指标已做类型、有限数和范围校验。

结果：

```text
uv run --locked --no-build pytest -q tests/unit/test_skill_judge_metrics.py tests/unit/test_grader_protocol.py tests/integration/test_sys02_isolated_grader.py
# 修复前：指标 22 failed / 20 passed；协议正确实现缺 trust；错误实现与 pytest 补丁得到 PASS
# 修复后：上述定向用例通过（含 Docker 评分容器）
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 199 passed
AETHER_REQUIRE_SERVICES=1 PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
# 328 passed，2 failed：Playwright chromium 未安装（本分支未改这两条用例）
# 安装 chromium 后单独重跑：2 passed。合计 330 passed（基线 325 + 本分支 5 条 SYS-02）
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
```

环境：Postgres 17.11 + pgvector 0.8.7，Temporal `temporalio/temporal:1.9.1`，`AETHER_REQUIRE_SERVICES=1`，`AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres`，`AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233`。本机没有 Node 24，未重跑 web typecheck（本分支不改 web）。

阻塞：无 live model key。不改 `live_ready`。`os.kill(-1, SIGKILL)` 只在评分容器里执行，宿主机测试不调用它。

未覆盖、不能称为已修复：容器逃逸、ptrace 父进程、猜中期望值、不能归约的白盒断言。SYS-01/03/04/05/06/08/09 不由该分支关闭。

决策：ADR-F30。

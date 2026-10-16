# SYS-02 协议评分与 SYS-07 指标校验

任务：关闭复审 `8b90363` 的 SYS-02 与 SYS-07。不改 SYS-01/03/04/06/08 所属文件。

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

未覆盖、不能称为已修复：容器逃逸、ptrace 父进程、猜中期望值、不能归约的白盒断言。SYS-01/03/04/05/06/08/09 不由本分支关闭。

决策：ADR-F30。

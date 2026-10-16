# SYS-01 补上 Watch 循环、采集续跑，并改编码说明

任务：在 `e2e4c6d` 上继续 SYS-01。ADR-F30 已由 SYS-02/07 占用，ADR-F31 已由 SYS-04/06 占用，ADR-F32 已由 SYS-08/03 占用，本决定记为 ADR-F33。同一 PR 关闭三处缺口：已批准的 Watch 用 Temporal schedule 循环；worker 重启后继续跑命令已 DELIVERED、状态仍为 QUEUED 的采集；`docs/08_api_ui.md` 的编码作业改为 unsupported。

相关合同：不改 `contracts/`、`reference/`、验收标准或 `STATUS.json`。不改变 `live_ready`。

拟改路径：

- `services/worker/aether_worker/service.py`
- `services/worker/aether_worker/watches.py`
- `services/worker/aether_worker/watches_workflow.py`
- `services/worker/aether_worker/watch_tick.py`
- `services/worker/aether_worker/pending_jobs.py`
- `services/worker/aether_worker/project_registry.py`
- `docs/08_api_ui.md`、`docs/DECISIONS.md`（ADR-F33）与由此重建的 `MASTER_SPEC.md`
- `tests/integration/test_sys01_entry.py`
- `tests/integration/test_sys01_restart.py`
- `tests/unit/test_api_ui_doc.py`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
uv run --locked --no-build python scripts/doctor.py --require-services
(cd apps/web && pnpm typecheck)
```

进度：三条新用例先失败。编码说明没有 unsupported；公开激活的 Watch 没有 Temporal schedule；延迟执行时采集直接 SUCCEEDED，没有停在 QUEUED。

结果：变基到 `e2e4c6d` 之后，冻结检查通过，参考套件 76，单元 227，集成 343，doctor exit 0，web typecheck exit 0（本机 Node 22）。

阻塞：无。不改 `live_ready`。github/arxiv 仍是显式 unsupported。水位不变时不发通知。

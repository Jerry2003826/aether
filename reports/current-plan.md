# R7-1 编码作业与技能评测

任务：实现编码作业和 `skill.evaluate`，去掉第六轮对这两条入口的 422 `unsupported`。变基到 `origin/main` `5fa78f7` 之后，ADR-F34 是公开采集，ADR-F35 是复核出口，本路决定改为 ADR-F36。ADR-F33 的 Watch 循环保留。

相关合同：不改 `contracts/`、`reference/`、验收门槛。`coding_jobs.state` 的成功值保持冻结的 `FINISHED`。不把 `live_ready` 改成 passed。不改 ADR-F34 的采集、工具和检索行为。

拟改路径：

- `services/api/aether_api/console_routes.py`
- `services/worker/aether_worker/console_commands.py`
- `services/worker/aether_worker/service.py`（采集和已批准工具之后跑编码作业与技能评测）
- `services/worker/aether_worker/coding_jobs.py`
- `services/worker/aether_worker/coding_sandbox.py`
- `services/worker/aether_worker/coding_workspace.py`
- `services/worker/aether_worker/bundle_files.py`
- `services/worker/aether_worker/skill_execute.py`
- `services/worker/aether_worker/skill_dataset.py`
- `services/worker/aether_worker/skill_metrics.py`
- `services/worker/aether_worker/skill_eval.py`（评测 id 与 manifest 状态）
- `apps/web/app/code/page.tsx`、`apps/web/app/console/page.tsx`
- `docs/08_api_ui.md`、`docs/DECISIONS.md` 与由此重建的 `MASTER_SPEC.md`
- `tests/unit/test_coding_skill_pipeline.py`、`tests/unit/test_api_ui_doc.py`
- `tests/integration/test_r7_coding_skill.py`、`test_console_api.py`、`test_console_records.py`、`test_console_browser.py`、`test_sys01_entry.py`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q tests/integration
(cd apps/web && pnpm typecheck)
```

进度：变基到 `5fa78f7` 后保留 ADR-F35 的复核出口，编码决定改记 ADR-F36。规格第 6 节的取消确认和 TTL 租约回收记为 ADR-F37。PR 审查意见记为 ADR-F38：围栏、同一作业键串行、日志尾部、async 测试失败、评测 `active_seconds`、基线绑定、不覆盖 CANARY/ACTIVE、安全失败优先，以及用现有任务事件查看终态。失败测试先写，再补实现。

结果：变基到 `5fa78f7` 之后，冻结检查通过，`api_operations` 仍是 50。参考套件 76。单元 244 passed（104.54s）。集成 368 passed、1 warning（360.79s）。web typecheck exit 0（本机 Node 22，CI 用 Node 24）。这次没有重跑 doctor。`reports/doctor.json` 没有提交。

阻塞：不启动 OpenHands，也不在沙箱里调用模型。没有新增编码作业 GET。`live_ready` 保持 pending。

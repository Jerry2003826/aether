# SYS-08 验收报告与 SYS-03 快照边界

任务：关掉云端复审的 SYS-08 和 SYS-03。分支 `cursor/r6-report-sys08-sys03-e789` 已变基到 `76fd71c`（PR #22）。ADR-F30 是评分隔离，ADR-F31 是记忆来源与验收条款。本分支的决定编号是 ADR-F32。

相关合同：不改 `contracts/` 与 `reference/`。不改历史 `reports/f26_acceptance_result.json`。不撤销评分隔离，不撤销记忆降级授权和草案条款冻结。

拟改路径：

- `services/worker/aether_worker/acceptance_report.py`
- `services/worker/aether_worker/snapshot.py`
- `services/worker/aether_worker/verification.py` 里的 `export_candidate`、`prepare_workspace`、`_stage`
- `tests/unit/test_acceptance_report.py`
- `tests/unit/test_snapshot_boundary.py`
- `STATUS.json` 的当前 live 字段
- `docs/DECISIONS.md`（ADR-F32）与由此重建的 `MASTER_SPEC.md`
- `reports/f27_criteria.md` 里描述旧假绿行为的那一行

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py' -q
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration
```

进度：失败用例、报告与快照已经接在 PR #21 之上。这次再变基到 PR #22，两边都留着。

结果：报告区分 pass、fail、not_run、blocked。空套件、全跳过、后段失败、计数不符、以及没有当前绑定的证据都不会变成 live_ready。三条快照路径拒绝符号链接。目录是 `0o750` 加 uid 65534 的命名用户 ACL。`readiness.live_ready` 是 pending。变基到 `76fd71c` 之后的全量结果待重跑。

阻塞：本机没有 live model key。所有者重跑 `scripts/run_f26_acceptance.py` 之前，live 保持 pending。

决策：ADR-F32。

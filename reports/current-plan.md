# F26 缺事实与引用失败分开记录

任务：比较报告没有写出数值 `100` 时，原因不要再记成 `citation_mismatch`。检查本身保持原样：报告必须包含 `100`。研究提示明确要求每条来源写出数值和单位。基线 `887444a`（PR #18 已合入）。分支 `cursor/f26-missing-fact-1f84`。

相关合同：不改 `contracts/` 与 `reference/`。

拟改路径：

- `services/worker/aether_worker/f26_acceptance.py`
- `tests/unit/test_f26_acceptance.py`
- `docs/DECISIONS.md`（ADR-F29）与由此重建的 `MASTER_SPEC.md`
- `reports/f26_criteria.md`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build pytest -q tests/unit
```

进度：先写了会在旧代码上失败的用例（缺数字被记成 `citation_mismatch`，提示里没有数值和单位的要求），再改判定和提示。`_report_cites` 里的 `100` 检查没有放宽。

结果：省略数字或写成「一百」时原因是 `missing_required_fact`，引用覆盖仍为否。数字还在但版本错误时仍是 `citation_mismatch`。`finish_reason=length` 仍优先。

阻塞：本机没有 live model key。`scripts/run_f26_acceptance.py` 须由所有者重跑。不改 `live_ready`。

决策：ADR-F29。

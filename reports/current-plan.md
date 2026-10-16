# F26 验收与研究路径加固

任务：收紧 F26 研究报告的版本引用解析，并让被长度截断的模型输出不能当成完整回答。基线 `0767a5e`。分支 `cursor/f26-hardening-1f84`。

相关合同：不改 `contracts/` 与 `reference/`。`finish_reason` 留在网关返回值和 `model_requests.usage_detail`，不进入 `ModelResult`。

拟改路径：

- `services/worker/aether_worker/f26_acceptance.py`
- `services/gateway/aether_gateway/model.py`
- `services/worker/aether_worker/goal_effect.py`
- `packages/storage/aether_storage/budget.py`
- `tests/unit/test_f26_acceptance.py`
- `tests/unit/test_model_truncation.py`
- `tests/integration/test_gateway.py`
- `tests/integration/test_live_research.py`
- `docs/DECISIONS.md`（ADR-F28）与由此重建的 `MASTER_SPEC.md`
- `reports/f26_criteria.md`

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
uv run --locked --no-build python scripts/doctor.py --require-services
```

进度：解析、网关、研究上限和 ADR 已落地。单元 145 通过。集成全量 324 通过，Hermes 身份探测在负载下一次 `commit=None`，单独重跑通过。参考套件 76 通过。`verify_freeze` 与 `doctor --require-services` 通过。

结果：错误版本、未绑定版本、缺少引用、`finish_reason=length` 都失败。四种新表述在版本号正确且绑到来源时通过。

阻塞：本机没有 live model key。`scripts/run_f26_acceptance.py` 与 `scripts/run_live_task_check.py` 须由所有者重跑。不改 `live_ready`。

决策：ADR-F28。研究报告单次 2048 token；编码和协议仍是 512。

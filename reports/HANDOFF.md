# 交接

F04–F06 在补齐每条验收的测试并通过后重新标为 done。对照表在 `reports/f04_f06_criteria.md`。

## 测试

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
60 passed
```

覆盖预算哈希冲突、并发 token 上限、连接重置、资源当前版本过期、上传流式限额和并发幂等。

## 状态

- F00、F02、F03、F04、F05、F06：done
- F01：blocked。BGE 权重未加载，`dimension_probed` 为 null。
- F07 done。`pytest -q tests/unit/test_planning.py tests/integration/test_workflow.py` → 7 passed。真实 Temporal 上去重 command，32 节点链、环、33 节点和读写并行限制由 planning 测试覆盖。
- 下一步：F08 最小真实 UI。

live 模型、CodeGraph、Hermes 仍是 not_run。

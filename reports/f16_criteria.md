# F16 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。tombstone 不会被写成物理删除。

| 验收 | 测试 |
|---|---|
| 旧的 embedding 任务不能在删除后写回向量；旧摘要不能改写已删记忆 | `tests/integration/test_retention.py::test_forgetting_blocks_late_writes_and_does_not_claim_physical_delete` |
| 记忆删除、向量清空、BM25 generation 退休、推荐特征快照失效一起发生 | `tests/integration/test_retention.py::test_forgetting_blocks_late_writes_and_does_not_claim_physical_delete` |
| 备份和 Temporal 历史的保留限制写在结果里，不能声称已经物理删除 | `tests/integration/test_retention.py::test_forgetting_blocks_late_writes_and_does_not_claim_physical_delete` |

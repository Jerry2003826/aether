# F16 记忆删除

目标：忘记一条记忆或一个 chunk 时先做 tombstone，抬高 visibility epoch，并让旧的向量和摘要任务写不回去。

合同：`docs/05_knowledge.md`，`docs/09_data.md`，`docs/14_BOUNDARIES_AND_FAILURES.md`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_retention.py`

进度：本地测试已通过。还没标 done。

阻塞：无。

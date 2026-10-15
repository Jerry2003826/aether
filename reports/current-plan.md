# F19 推荐

目标：可解释排序、去重反馈、默认关闭探索。隐藏和删除立刻影响候选。清空画像后可重建。不称协同过滤。

合同：`docs/05_knowledge.md`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_recommendations.py`

进度：本地 1 passed。还没标 done。

阻塞：无。

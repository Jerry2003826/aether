# F19 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。排序只用内容特征，不称协同过滤。

| 验收 | 测试 |
|---|---|
| 曝光和反馈都有编号，重复事件不重复计入 | `tests/integration/test_recommendations.py::test_feedback_is_deduped_and_exploration_records_probability` |
| 默认不探索；打开时记录候选概率和策略版本 | `tests/integration/test_recommendations.py::test_feedback_is_deduped_and_exploration_records_probability` |
| 隐藏或删除立刻影响候选；清空画像后仍可重建；不称协同过滤 | `tests/integration/test_recommendations.py::test_feedback_is_deduped_and_exploration_records_probability` |

# F12 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。

| 验收 | 测试 |
|---|---|
| 来源声称、推断、用户确认和提议分开保存 | `tests/integration/test_memory.py::test_memory_statuses_stay_separate_and_conflicts_do_not_overwrite` |
| 创建接口不能把记忆直接标成用户确认 | `tests/unit/test_context.py::test_confirmation_is_not_a_create_status` |
| 没有测试产物的 observed_test 降为 proposed | `tests/unit/test_context.py::test_confirmation_is_not_a_create_status` |
| 冲突不改写原说法 | `tests/integration/test_memory.py::test_memory_statuses_stay_separate_and_conflicts_do_not_overwrite` |
| 密钥授权不会被写进记忆 | `tests/integration/test_memory.py::test_memory_statuses_stay_separate_and_conflicts_do_not_overwrite` |
| 上下文按预算组装目标、计划和证据，不回灌全量历史 | `tests/unit/test_context.py::test_context_budget_drops_history_and_unauthorized_text` |
| 未选中或未审核的技能指令不进入上下文 | `tests/unit/test_context.py::test_context_budget_drops_history_and_unauthorized_text` |
| 高敏摘要和跨项目 CodeGraph 不能绕过出口授权 | `tests/unit/test_context.py::test_context_budget_drops_history_and_unauthorized_text` |
| 另一项目读不到本项目记忆，历史和跨项目图谱不进入组装结果 | `tests/integration/test_memory.py::test_memory_statuses_stay_separate_and_conflicts_do_not_overwrite` |
